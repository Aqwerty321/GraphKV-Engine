use std::borrow::Cow;

use anyhow::{anyhow, Context, Result};
use async_nats::Client as NatsClient;
use bytes::Bytes;
use futures_util::StreamExt;
use reqwest::Client as HttpClient;
use tokio::sync::mpsc;

use crate::{
    egress::{send_enriched_chunk, DEFAULT_GRAPHKV_DOC_WS_URL},
    embedder::{build_http_client, embed_text_chunks, EmbeddingConfig},
    models::{
        parse_reasoning_completed, EnrichedChunk, RawChunk, ReasoningRequest, DEFAULT_NATS_URL,
        REASONING_COMPLETED_SUBJECT, REASONING_REQUESTS_SUBJECT,
    },
};

#[derive(Debug, Clone)]
pub struct RouterConfig<'a> {
    pub nats_url: Cow<'a, str>,
    pub websocket_url: Cow<'a, str>,
    pub embedding: EmbeddingConfig<'a>,
}

impl RouterConfig<'static> {
    pub fn local(embedding_model: impl Into<Cow<'static, str>>) -> Self {
        Self {
            nats_url: Cow::Borrowed(DEFAULT_NATS_URL),
            websocket_url: Cow::Borrowed(DEFAULT_GRAPHKV_DOC_WS_URL),
            embedding: EmbeddingConfig::local(embedding_model),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteTarget {
    LocalEmbed,
    Reasoner,
}

pub fn route_target(chunk: &RawChunk<'_>) -> RouteTarget {
    if chunk.is_source_code() {
        RouteTarget::LocalEmbed
    } else {
        RouteTarget::Reasoner
    }
}

pub async fn run_router(
    mut incoming_chunks: mpsc::Receiver<RawChunk<'static>>,
    config: RouterConfig<'_>,
) -> Result<()> {
    let nats = async_nats::connect(config.nats_url.as_ref())
        .await
        .with_context(|| format!("failed to connect NATS at {}", config.nats_url))?;
    let mut completed = nats
        .subscribe(REASONING_COMPLETED_SUBJECT.to_owned())
        .await
        .context("failed to subscribe to completed reasoning subject")?;
    let http_client = build_http_client(config.embedding.timeout)?;
    let mut incoming_open = true;

    loop {
        tokio::select! {
            maybe_chunk = incoming_chunks.recv(), if incoming_open => {
                match maybe_chunk {
                    Some(chunk) => {
                        route_raw_chunk(
                            &nats,
                            &http_client,
                            &config.embedding,
                            config.websocket_url.as_ref(),
                            chunk,
                        )
                        .await?;
                    }
                    None => incoming_open = false,
                }
            }
            maybe_message = completed.next() => {
                let message = maybe_message.ok_or_else(|| anyhow!("NATS completed reasoning subscription closed"))?;
                handle_completed_payload(config.websocket_url.as_ref(), message.payload.as_ref()).await?;
            }
        }
    }
}

pub async fn route_raw_chunk(
    nats: &NatsClient,
    http_client: &HttpClient,
    embedding_config: &EmbeddingConfig<'_>,
    websocket_url: &str,
    chunk: RawChunk<'static>,
) -> Result<()> {
    match route_target(&chunk) {
        RouteTarget::LocalEmbed => {
            embed_and_egress_source_chunk(http_client, embedding_config, websocket_url, chunk).await
        }
        RouteTarget::Reasoner => publish_reasoning_request(nats, chunk).await,
    }
}

pub async fn publish_reasoning_request(nats: &NatsClient, chunk: RawChunk<'static>) -> Result<()> {
    let request_id = Cow::Owned(chunk.chunk_id.to_string());
    let request = ReasoningRequest::new(request_id, chunk);
    let payload =
        serde_json::to_vec(&request).context("failed to serialize NATS reasoning request")?;
    nats.publish(REASONING_REQUESTS_SUBJECT.to_owned(), Bytes::from(payload))
        .await
        .context("failed to publish NATS reasoning request")?;
    Ok(())
}

async fn embed_and_egress_source_chunk(
    http_client: &HttpClient,
    embedding_config: &EmbeddingConfig<'_>,
    websocket_url: &str,
    chunk: RawChunk<'static>,
) -> Result<()> {
    let input = [Cow::Borrowed(chunk.text.as_ref())];
    let mut embeddings = embed_text_chunks(http_client, embedding_config, &input).await?;
    let embedding = embeddings.pop().ok_or_else(|| {
        anyhow!(
            "local embedder returned no vector for source chunk {}",
            chunk.chunk_id
        )
    })?;
    let enriched = EnrichedChunk::new(chunk, Cow::Borrowed(""), embedding)?;
    send_enriched_chunk(websocket_url, &enriched).await
}

pub async fn handle_completed_payload(websocket_url: &str, payload: &[u8]) -> Result<()> {
    let completed = parse_reasoning_completed(payload)
        .context("failed to deserialize completed reasoning NATS payload")?;
    completed.chunk.validate()?;
    send_enriched_chunk(websocket_url, &completed.chunk).await
}

#[cfg(test)]
mod tests {
    use std::borrow::Cow;

    use super::*;
    use crate::models::{ChunkKind, RawChunk};

    fn raw(chunk_type: ChunkKind) -> RawChunk<'static> {
        RawChunk {
            chunk_id: Cow::Borrowed("chunk"),
            document_id: Cow::Borrowed("doc"),
            path: Cow::Borrowed("README.md"),
            heading: Cow::Borrowed(""),
            language: Cow::Borrowed("markdown"),
            chunk_type,
            text: Cow::Borrowed("content"),
        }
    }

    #[test]
    fn source_code_bypasses_reasoner() {
        assert_eq!(
            route_target(&raw(ChunkKind::SourceCode)),
            RouteTarget::LocalEmbed
        );
    }

    #[test]
    fn documentation_routes_to_reasoner() {
        assert_eq!(
            route_target(&raw(ChunkKind::Documentation)),
            RouteTarget::Reasoner
        );
    }
}
