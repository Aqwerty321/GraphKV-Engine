use anyhow::{ensure, Context, Result};
use bytes::Bytes;
use futures_util::SinkExt;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::models::{DocChunkRecord, EnrichedChunk, EMBEDDING_DIMENSIONS};

pub const DEFAULT_GRAPHKV_DOC_WS_URL: &str = "ws://127.0.0.1:8888/v1/ingest_docs";
pub const ZSTD_COMPRESSION_LEVEL: i32 = 3;

pub fn serialize_and_compress(records: &[DocChunkRecord]) -> Result<Bytes> {
    for record in records {
        ensure!(
            record.embedding.len() == EMBEDDING_DIMENSIONS,
            "chunk {} embedding dimension mismatch at egress: got {}, expected {EMBEDDING_DIMENSIONS}",
            record.chunk_id,
            record.embedding.len()
        );
    }

    let serialized =
        serde_json::to_vec(records).context("failed to serialize outbound doc chunk records")?;
    let compressed = zstd::stream::encode_all(serialized.as_slice(), ZSTD_COMPRESSION_LEVEL)
        .context("failed to zstd-compress outbound doc chunk records")?;
    Ok(Bytes::from(compressed))
}

pub fn serialize_enriched_and_compress(chunk: &EnrichedChunk<'_>) -> Result<Bytes> {
    chunk.validate()?;
    let serialized =
        serde_json::to_vec(chunk).context("failed to serialize outbound enriched chunk")?;
    let compressed = zstd::stream::encode_all(serialized.as_slice(), ZSTD_COMPRESSION_LEVEL)
        .context("failed to zstd-compress outbound enriched chunk")?;
    Ok(Bytes::from(compressed))
}

async fn send_compressed_payload(websocket_url: &str, payload: Bytes) -> Result<()> {
    let (mut socket, _) = connect_async(websocket_url).await.with_context(|| {
        format!("failed to connect GraphKV WebSocket egress route at {websocket_url}")
    })?;

    socket
        .send(Message::Binary(payload))
        .await
        .context("failed to send compressed doc payload to GraphKV")?;
    socket
        .close(None)
        .await
        .context("failed to close GraphKV WebSocket egress route cleanly")?;
    Ok(())
}

pub async fn send_enriched_chunk(websocket_url: &str, chunk: &EnrichedChunk<'_>) -> Result<()> {
    let payload = serialize_enriched_and_compress(chunk)?;
    send_compressed_payload(websocket_url, payload).await
}

pub async fn send_chunk_records(websocket_url: &str, records: &[DocChunkRecord]) -> Result<()> {
    let payload = serialize_and_compress(records)?;
    send_compressed_payload(websocket_url, payload).await
}

#[cfg(test)]
mod tests {
    use std::borrow::Cow;

    use crate::models::{ChunkKind, RawChunk, StringRef};

    use super::*;
    use futures_util::StreamExt;
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;

    #[test]
    fn egress_revalidates_embedding_dimensions() -> anyhow::Result<()> {
        let bad = DocChunkRecord {
            chunk_id: 42,
            text_ref: StringRef {
                offset: 0,
                length: 8,
            },
            embedding: vec![0.0; 8],
        };

        let result = serialize_and_compress(&[bad]);
        assert!(result.is_err());
        Ok(())
    }

    #[tokio::test]
    async fn websocket_egress_sends_zstd_binary_payload() -> anyhow::Result<()> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await?;
            let mut websocket = accept_async(stream).await?;
            let message = websocket
                .next()
                .await
                .ok_or_else(|| anyhow::anyhow!("client closed before sending payload"))??;
            let Message::Binary(payload) = message else {
                anyhow::bail!("expected binary websocket payload");
            };
            let decoded = zstd::stream::decode_all(payload.as_ref())?;
            let records: Vec<DocChunkRecord> = serde_json::from_slice(&decoded)?;
            anyhow::ensure!(records.len() == 1, "expected one record");
            anyhow::ensure!(records[0].chunk_id == 7, "unexpected chunk id");
            anyhow::ensure!(
                records[0].embedding.len() == EMBEDDING_DIMENSIONS,
                "unexpected embedding dimensions"
            );
            Ok::<_, anyhow::Error>(())
        });

        let record = DocChunkRecord::new(
            7,
            StringRef {
                offset: 16,
                length: 32,
            },
            vec![0.5; EMBEDDING_DIMENSIONS],
        )?;
        send_chunk_records(&format!("ws://{address}/v1/ingest_docs"), &[record]).await?;
        server.await??;
        Ok(())
    }

    #[tokio::test]
    async fn enriched_egress_sends_zstd_binary_payload() -> anyhow::Result<()> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await?;
            let mut websocket = accept_async(stream).await?;
            let message = websocket
                .next()
                .await
                .ok_or_else(|| anyhow::anyhow!("client closed before sending payload"))??;
            let Message::Binary(payload) = message else {
                anyhow::bail!("expected binary websocket payload");
            };
            let decoded = zstd::stream::decode_all(payload.as_ref())?;
            let enriched: EnrichedChunk<'_> = serde_json::from_slice(&decoded)?;
            anyhow::ensure!(enriched.raw.chunk_id == "chunk-7", "unexpected chunk id");
            anyhow::ensure!(
                enriched.embedding.len() == EMBEDDING_DIMENSIONS,
                "unexpected embedding dimensions"
            );
            Ok::<_, anyhow::Error>(())
        });

        let chunk = EnrichedChunk::new(
            RawChunk {
                chunk_id: Cow::Borrowed("chunk-7"),
                document_id: Cow::Borrowed("doc-1"),
                path: Cow::Borrowed("README.md"),
                heading: Cow::Borrowed("Intro"),
                language: Cow::Borrowed("markdown"),
                chunk_type: ChunkKind::Documentation,
                text: Cow::Borrowed("GraphKV context"),
            },
            Cow::Borrowed("Short prefix"),
            vec![0.5; EMBEDDING_DIMENSIONS],
        )?;
        send_enriched_chunk(&format!("ws://{address}/v1/ingest_docs"), &chunk).await?;
        server.await??;
        Ok(())
    }
}
