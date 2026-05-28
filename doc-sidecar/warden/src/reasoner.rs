use std::{borrow::Cow, time::Duration};

use anyhow::{anyhow, bail, ensure, Context, Result};
use async_nats::Client as NatsClient;
use bytes::Bytes;
use futures_util::StreamExt;
use reqwest::{header::CONTENT_TYPE, Client as HttpClient};
use serde::{Deserialize, Serialize};
use tokio::time::timeout;

use crate::{
    embedder::{build_http_client, embed_text_chunks, EmbeddingConfig},
    models::{
        parse_reasoning_request, EnrichedChunk, RawChunk, ReasoningCompleted, ReasoningRequest,
        DEFAULT_NATS_URL, REASONING_COMPLETED_SUBJECT, REASONING_REQUESTS_SUBJECT,
    },
};

pub const DEFAULT_VLLM_COMPLETIONS_ENDPOINT: &str = "http://127.0.0.1:8000/v1/completions";
pub const DEFAULT_VLLM_MODEL: &str = "local-vllm";
pub const DEFAULT_REASONER_BATCH_SIZE: usize = 100;
pub const DEFAULT_REASONER_BATCH_TIMEOUT: Duration = Duration::from_millis(500);
pub const DEFAULT_REASONER_REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
pub const DEFAULT_CONTEXT_PREFIX_WORDS: u32 = 50;

#[derive(Debug, Clone)]
pub struct ReasonerConfig<'a> {
    pub nats_url: Cow<'a, str>,
    pub completion_endpoint: Cow<'a, str>,
    pub completion_model: Cow<'a, str>,
    pub embedding: EmbeddingConfig<'a>,
    pub batch_size: usize,
    pub batch_timeout: Duration,
    pub request_timeout: Duration,
    pub prefix_words: u32,
}

impl ReasonerConfig<'static> {
    pub fn local(embedding_model: impl Into<Cow<'static, str>>) -> Self {
        Self {
            nats_url: Cow::Borrowed(DEFAULT_NATS_URL),
            completion_endpoint: Cow::Borrowed(DEFAULT_VLLM_COMPLETIONS_ENDPOINT),
            completion_model: Cow::Borrowed(DEFAULT_VLLM_MODEL),
            embedding: EmbeddingConfig::local(embedding_model),
            batch_size: DEFAULT_REASONER_BATCH_SIZE,
            batch_timeout: DEFAULT_REASONER_BATCH_TIMEOUT,
            request_timeout: DEFAULT_REASONER_REQUEST_TIMEOUT,
            prefix_words: DEFAULT_CONTEXT_PREFIX_WORDS,
        }
    }
}

#[derive(Debug, Serialize)]
struct VllmCompletionRequest<'a> {
    model: Cow<'a, str>,
    prompt: &'a [String],
    max_tokens: u32,
    temperature: f32,
}

#[derive(Debug, Deserialize)]
struct VllmCompletionResponse<'a> {
    #[serde(borrow, default)]
    choices: Vec<VllmCompletionChoice<'a>>,
}

#[derive(Debug, Deserialize)]
struct VllmCompletionChoice<'a> {
    #[serde(default)]
    index: Option<usize>,
    #[serde(borrow, default)]
    text: Cow<'a, str>,
}

pub async fn run_reasoner(config: ReasonerConfig<'_>) -> Result<()> {
    ensure!(
        config.batch_size > 0,
        "reasoner batch size must be greater than zero"
    );
    let nats = async_nats::connect(config.nats_url.as_ref())
        .await
        .with_context(|| format!("failed to connect NATS at {}", config.nats_url))?;
    let mut requests = nats
        .subscribe(REASONING_REQUESTS_SUBJECT.to_owned())
        .await
        .context("failed to subscribe to reasoning request subject")?;
    let http_client = build_http_client(max_duration(
        config.request_timeout,
        config.embedding.timeout,
    ))?;

    loop {
        let first = requests
            .next()
            .await
            .ok_or_else(|| anyhow!("NATS reasoning request subscription closed"))?;
        let mut batch = Vec::with_capacity(config.batch_size);
        batch.push(parse_request_payload(first.payload.as_ref())?);

        let batch_deadline = tokio::time::sleep(config.batch_timeout);
        tokio::pin!(batch_deadline);
        while batch.len() < config.batch_size {
            tokio::select! {
                maybe_message = requests.next() => {
                    let message = maybe_message.ok_or_else(|| anyhow!("NATS reasoning request subscription closed"))?;
                    batch.push(parse_request_payload(message.payload.as_ref())?);
                }
                _ = &mut batch_deadline => break,
            }
        }

        process_reasoning_batch(&http_client, &nats, &config, batch).await?;
    }
}

fn parse_request_payload(payload: &[u8]) -> Result<ReasoningRequest<'static>> {
    let request = parse_reasoning_request(payload)
        .context("failed to deserialize NATS reasoning request payload")?;
    Ok(request.into_owned())
}

pub async fn process_reasoning_batch(
    http_client: &HttpClient,
    nats: &NatsClient,
    config: &ReasonerConfig<'_>,
    batch: Vec<ReasoningRequest<'static>>,
) -> Result<()> {
    if batch.is_empty() {
        return Ok(());
    }

    let prefixes = request_context_prefixes(http_client, config, &batch).await?;
    let embedding_inputs: Vec<Cow<'_, str>> = batch
        .iter()
        .zip(prefixes.iter())
        .map(|(request, prefix)| {
            Cow::Owned(join_prefix_and_text(prefix, request.chunk.text.as_ref()))
        })
        .collect();
    let embeddings = embed_text_chunks(http_client, &config.embedding, &embedding_inputs).await?;
    ensure!(
        embeddings.len() == batch.len(),
        "llama.cpp returned {} embeddings for {} reasoned chunks",
        embeddings.len(),
        batch.len()
    );

    for ((request, prefix), embedding) in batch.into_iter().zip(prefixes).zip(embeddings) {
        let enriched = EnrichedChunk::new(request.chunk, Cow::Owned(prefix), embedding)?;
        let completed = ReasoningCompleted::new(request.request_id, enriched);
        let payload = serde_json::to_vec(&completed)
            .context("failed to serialize completed reasoning NATS payload")?;
        nats.publish(REASONING_COMPLETED_SUBJECT.to_owned(), Bytes::from(payload))
            .await
            .context("failed to publish completed reasoning payload")?;
    }

    Ok(())
}

async fn request_context_prefixes(
    http_client: &HttpClient,
    config: &ReasonerConfig<'_>,
    batch: &[ReasoningRequest<'static>],
) -> Result<Vec<String>> {
    let prompts: Vec<String> = batch
        .iter()
        .map(|request| build_context_prompt(&request.chunk, config.prefix_words))
        .collect();
    let request = VllmCompletionRequest {
        model: Cow::Borrowed(config.completion_model.as_ref()),
        prompt: &prompts,
        max_tokens: config.prefix_words.saturating_mul(3).max(32),
        temperature: 0.0,
    };
    let body =
        serde_json::to_vec(&request).context("failed to serialize vLLM completion request")?;

    let response = timeout(
        config.request_timeout,
        http_client
            .post(config.completion_endpoint.as_ref())
            .header(CONTENT_TYPE, "application/json")
            .body(body)
            .send(),
    )
    .await
    .map_err(|_| {
        anyhow!(
            "vLLM completion request timed out after {:?}",
            config.request_timeout
        )
    })?
    .context("vLLM completion request failed")?;

    let status = response.status();
    if !status.is_success() {
        let error_body = match response.text().await {
            Ok(body) => body,
            Err(error) => format!("<body unavailable: {error}>"),
        };
        bail!("vLLM completion request returned {status}: {error_body}");
    }

    let response_bytes = response
        .bytes()
        .await
        .context("failed to read vLLM completion response body")?;
    parse_completion_prefixes(&response_bytes, batch.len())
}

pub fn build_context_prompt(chunk: &RawChunk<'_>, prefix_words: u32) -> String {
    format!(
        "Write a dense retrieval context prefix of at most {prefix_words} words. Return only the prefix.\nPath: {}\nHeading: {}\nText:\n{}",
        chunk.path, chunk.heading, chunk.text
    )
}

pub fn join_prefix_and_text(prefix: &str, raw_text: &str) -> String {
    let trimmed_prefix = prefix.trim();
    if trimmed_prefix.is_empty() {
        format!("[Raw Text]\n{raw_text}")
    } else {
        format!("[Prefix]\n{trimmed_prefix}\n\n[Raw Text]\n{raw_text}")
    }
}

fn parse_completion_prefixes(input: &[u8], expected: usize) -> Result<Vec<String>> {
    let parsed: VllmCompletionResponse<'_> =
        serde_json::from_slice(input).context("failed to deserialize vLLM completion response")?;
    ensure!(
        parsed.choices.len() == expected,
        "vLLM returned {} prefixes for {} chunks",
        parsed.choices.len(),
        expected
    );

    let mut ordered = vec![None; expected];
    for (position, choice) in parsed.choices.into_iter().enumerate() {
        let index = match choice.index {
            Some(index) => index,
            None => position,
        };
        let slot = ordered
            .get_mut(index)
            .ok_or_else(|| anyhow!("vLLM returned out-of-range choice index {index}"))?;
        ensure!(
            slot.is_none(),
            "vLLM returned duplicate choice index {index}"
        );
        *slot = Some(choice.text.trim().to_owned());
    }

    ordered
        .into_iter()
        .enumerate()
        .map(|(index, prefix)| prefix.ok_or_else(|| anyhow!("vLLM omitted choice index {index}")))
        .collect()
}

fn max_duration(left: Duration, right: Duration) -> Duration {
    if left >= right {
        left
    } else {
        right
    }
}

#[cfg(test)]
mod tests {
    use std::borrow::Cow;

    use super::*;
    use crate::models::ChunkKind;

    fn request(chunk_id: &'static str) -> ReasoningRequest<'static> {
        ReasoningRequest::new(
            Cow::Borrowed(chunk_id),
            RawChunk {
                chunk_id: Cow::Borrowed(chunk_id),
                document_id: Cow::Borrowed("doc"),
                path: Cow::Borrowed("README.md"),
                heading: Cow::Borrowed("Install"),
                language: Cow::Borrowed("markdown"),
                chunk_type: ChunkKind::Documentation,
                text: Cow::Borrowed("GraphKV accepts document chunks."),
            },
        )
    }

    #[test]
    fn context_prompt_includes_location_and_text() {
        let prompt = build_context_prompt(&request("chunk-1").chunk, 50);
        assert!(prompt.contains("README.md"));
        assert!(prompt.contains("Install"));
        assert!(prompt.contains("GraphKV accepts document chunks."));
    }

    #[test]
    fn prefix_join_keeps_prefix_before_raw_text() {
        let joined = join_prefix_and_text("retrieval hint", "raw body");
        assert!(joined.starts_with("[Prefix]\nretrieval hint"));
        assert!(joined.contains("[Raw Text]\nraw body"));
    }

    #[test]
    fn completion_prefixes_are_ordered_by_choice_index() -> anyhow::Result<()> {
        let body = br#"{"choices":[{"index":1,"text":" second "},{"index":0,"text":"first"}]}"#;
        let prefixes = parse_completion_prefixes(body, 2)?;
        assert_eq!(prefixes, vec!["first".to_owned(), "second".to_owned()]);
        Ok(())
    }
}
