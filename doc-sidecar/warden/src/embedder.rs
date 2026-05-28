use std::{borrow::Cow, time::Duration};

use anyhow::{anyhow, bail, ensure, Context, Result};
use reqwest::{header::CONTENT_TYPE, Client};
use tokio::time::timeout;

use crate::models::{LlamaEmbeddingRequest, LlamaEmbeddingResponse, EMBEDDING_DIMENSIONS};

pub const DEFAULT_LLAMA_EMBEDDING_ENDPOINT: &str = "http://127.0.0.1:8001/v1/embeddings";
pub const DEFAULT_LLAMA_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
pub struct EmbeddingConfig<'a> {
    pub endpoint: Cow<'a, str>,
    pub model: Cow<'a, str>,
    pub timeout: Duration,
}

impl<'a> EmbeddingConfig<'a> {
    pub fn local(model: impl Into<Cow<'a, str>>) -> Self {
        Self {
            endpoint: Cow::Borrowed(DEFAULT_LLAMA_EMBEDDING_ENDPOINT),
            model: model.into(),
            timeout: DEFAULT_LLAMA_TIMEOUT,
        }
    }
}

pub fn build_http_client(timeout_duration: Duration) -> Result<Client> {
    Client::builder()
        .connect_timeout(timeout_duration)
        .timeout(timeout_duration)
        .build()
        .context("failed to build reqwest client for llama.cpp embeddings")
}

pub async fn embed_text_chunks<'a>(
    client: &Client,
    config: &EmbeddingConfig<'_>,
    chunks: &'a [Cow<'a, str>],
) -> Result<Vec<Vec<f32>>> {
    if chunks.is_empty() {
        return Ok(Vec::new());
    }

    let request = LlamaEmbeddingRequest {
        model: Cow::Borrowed(config.model.as_ref()),
        input: chunks,
    };
    let body =
        serde_json::to_vec(&request).context("failed to serialize llama.cpp embedding request")?;

    let response = timeout(
        config.timeout,
        client
            .post(config.endpoint.as_ref())
            .header(CONTENT_TYPE, "application/json")
            .body(body)
            .send(),
    )
    .await
    .map_err(|_| {
        anyhow!(
            "llama.cpp embedding request timed out after {:?}",
            config.timeout
        )
    })?
    .context("llama.cpp embedding request failed")?;

    let status = response.status();
    if !status.is_success() {
        let error_body = match response.text().await {
            Ok(body) => body,
            Err(error) => format!("<body unavailable: {error}>"),
        };
        bail!("llama.cpp embedding request returned {status}: {error_body}");
    }

    let response_bytes = response
        .bytes()
        .await
        .context("failed to read llama.cpp embedding response body")?;
    let parsed: LlamaEmbeddingResponse<'_> = serde_json::from_slice(&response_bytes)
        .context("failed to deserialize llama.cpp embedding response")?;

    ensure!(
        parsed.data.len() == chunks.len(),
        "llama.cpp returned {} embeddings for {} chunks",
        parsed.data.len(),
        chunks.len()
    );

    let mut ordered = vec![None; chunks.len()];
    for item in parsed.data {
        ensure!(
            item.index < chunks.len(),
            "llama.cpp returned out-of-range embedding index {} for {} chunks",
            item.index,
            chunks.len()
        );
        ensure!(
            item.embedding.len() == EMBEDDING_DIMENSIONS,
            "embedding {} dimension mismatch: got {}, expected {EMBEDDING_DIMENSIONS}",
            item.index,
            item.embedding.len()
        );
        if ordered[item.index].replace(item.embedding).is_some() {
            bail!(
                "llama.cpp returned duplicate embedding index {}",
                item.index
            );
        }
    }

    ordered
        .into_iter()
        .enumerate()
        .map(|(index, embedding)| {
            embedding.ok_or_else(|| anyhow!("llama.cpp omitted embedding index {index}"))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use std::borrow::Cow;

    use super::*;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
    };

    async fn read_http_request(stream: &mut TcpStream) -> anyhow::Result<Vec<u8>> {
        let mut request = Vec::new();
        let mut buffer = [0_u8; 1024];
        loop {
            let read = stream.read(&mut buffer).await?;
            if read == 0 {
                break;
            }
            request.extend_from_slice(&buffer[..read]);
            if request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
        }
        Ok(request)
    }

    async fn spawn_embedding_server(body: String, status: &str) -> anyhow::Result<String> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let status = status.to_owned();
        tokio::spawn(async move {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            if read_http_request(&mut stream).await.is_err() {
                return;
            }
            let response = format!(
                "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes()).await;
        });
        Ok(format!("http://{address}/v1/embeddings"))
    }

    #[test]
    fn empty_batch_does_not_call_network() -> anyhow::Result<()> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()?;
        let client = build_http_client(Duration::from_millis(50))?;
        let config = EmbeddingConfig::local("local-embedder");
        let chunks: Vec<Cow<'_, str>> = Vec::new();
        let vectors = runtime.block_on(embed_text_chunks(&client, &config, &chunks))?;
        assert!(vectors.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn embedding_bridge_accepts_ordered_1024d_vectors() -> anyhow::Result<()> {
        let body = format!(
            r#"{{"object":"list","data":[{{"index":1,"embedding":{one}}},{{"index":0,"embedding":{zero}}}],"model":"local"}}"#,
            zero = serde_json::to_string(&vec![0.0_f32; EMBEDDING_DIMENSIONS])?,
            one = serde_json::to_string(&vec![1.0_f32; EMBEDDING_DIMENSIONS])?
        );
        let endpoint = spawn_embedding_server(body, "200 OK").await?;
        let client = build_http_client(Duration::from_secs(2))?;
        let config = EmbeddingConfig {
            endpoint: Cow::Owned(endpoint),
            model: Cow::Borrowed("local"),
            timeout: Duration::from_secs(2),
        };
        let chunks = vec![Cow::Borrowed("alpha"), Cow::Borrowed("beta")];

        let embeddings = embed_text_chunks(&client, &config, &chunks).await?;

        assert_eq!(embeddings.len(), 2);
        assert_eq!(embeddings[0][0], 0.0);
        assert_eq!(embeddings[1][0], 1.0);
        Ok(())
    }

    #[tokio::test]
    async fn embedding_bridge_rejects_wrong_dimensions() -> anyhow::Result<()> {
        let body = format!(
            r#"{{"data":[{{"index":0,"embedding":{embedding}}}]}}"#,
            embedding = serde_json::to_string(&vec![0.0_f32; EMBEDDING_DIMENSIONS - 1])?
        );
        let endpoint = spawn_embedding_server(body, "200 OK").await?;
        let client = build_http_client(Duration::from_secs(2))?;
        let config = EmbeddingConfig {
            endpoint: Cow::Owned(endpoint),
            model: Cow::Borrowed("local"),
            timeout: Duration::from_secs(2),
        };
        let chunks = vec![Cow::Borrowed("alpha")];

        let result = embed_text_chunks(&client, &config, &chunks).await;

        assert!(result.is_err());
        Ok(())
    }

    #[tokio::test]
    async fn embedding_bridge_times_out_gracefully() -> anyhow::Result<()> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        tokio::spawn(async move {
            let Ok((_stream, _)) = listener.accept().await else {
                return;
            };
            tokio::time::sleep(Duration::from_secs(1)).await;
        });
        let client = build_http_client(Duration::from_millis(50))?;
        let config = EmbeddingConfig {
            endpoint: Cow::Owned(format!("http://{address}/v1/embeddings")),
            model: Cow::Borrowed("local"),
            timeout: Duration::from_millis(50),
        };
        let chunks = vec![Cow::Borrowed("alpha")];

        let result = embed_text_chunks(&client, &config, &chunks).await;

        assert!(result.is_err());
        Ok(())
    }
}
