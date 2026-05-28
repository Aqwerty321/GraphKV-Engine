use std::borrow::Cow;

use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};

pub const EMBEDDING_DIMENSIONS: usize = 1024;
pub const NATS_SCHEMA_VERSION: u16 = 1;
pub const DEFAULT_NATS_URL: &str = "nats://127.0.0.1:4222";
pub const REASONING_REQUESTS_SUBJECT: &str = "graphkv.reasoning.requests";
pub const REASONING_COMPLETED_SUBJECT: &str = "graphkv.reasoning.completed";

#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChunkKind {
    SourceCode,
    #[default]
    Documentation,
    Other,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RawChunk<'a> {
    #[serde(borrow, alias = "id")]
    pub chunk_id: Cow<'a, str>,
    #[serde(borrow, default)]
    pub document_id: Cow<'a, str>,
    #[serde(borrow, default)]
    pub path: Cow<'a, str>,
    #[serde(borrow, default)]
    pub heading: Cow<'a, str>,
    #[serde(borrow, default)]
    pub language: Cow<'a, str>,
    #[serde(rename = "type", alias = "chunk_type", default)]
    pub chunk_type: ChunkKind,
    #[serde(borrow, alias = "content")]
    pub text: Cow<'a, str>,
}

impl<'a> RawChunk<'a> {
    pub fn is_source_code(&self) -> bool {
        self.chunk_type == ChunkKind::SourceCode
    }

    pub fn into_owned(self) -> RawChunk<'static> {
        RawChunk {
            chunk_id: Cow::Owned(self.chunk_id.into_owned()),
            document_id: Cow::Owned(self.document_id.into_owned()),
            path: Cow::Owned(self.path.into_owned()),
            heading: Cow::Owned(self.heading.into_owned()),
            language: Cow::Owned(self.language.into_owned()),
            chunk_type: self.chunk_type,
            text: Cow::Owned(self.text.into_owned()),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EnrichedChunk<'a> {
    #[serde(borrow)]
    pub raw: RawChunk<'a>,
    #[serde(borrow)]
    pub context_prefix: Cow<'a, str>,
    pub embedding: Vec<f32>,
}

impl<'a> EnrichedChunk<'a> {
    pub fn new(
        raw: RawChunk<'a>,
        context_prefix: impl Into<Cow<'a, str>>,
        embedding: Vec<f32>,
    ) -> Result<Self> {
        ensure!(
            embedding.len() == EMBEDDING_DIMENSIONS,
            "chunk {} enriched embedding dimension mismatch: got {}, expected {EMBEDDING_DIMENSIONS}",
            raw.chunk_id,
            embedding.len()
        );
        Ok(Self {
            raw,
            context_prefix: context_prefix.into(),
            embedding,
        })
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.embedding.len() == EMBEDDING_DIMENSIONS,
            "chunk {} enriched embedding dimension mismatch: got {}, expected {EMBEDDING_DIMENSIONS}",
            self.raw.chunk_id,
            self.embedding.len()
        );
        Ok(())
    }

    pub fn into_owned(self) -> EnrichedChunk<'static> {
        EnrichedChunk {
            raw: self.raw.into_owned(),
            context_prefix: Cow::Owned(self.context_prefix.into_owned()),
            embedding: self.embedding,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ReasoningRequest<'a> {
    pub schema_version: u16,
    #[serde(borrow)]
    pub request_id: Cow<'a, str>,
    #[serde(borrow)]
    pub chunk: RawChunk<'a>,
}

impl<'a> ReasoningRequest<'a> {
    pub fn new(request_id: impl Into<Cow<'a, str>>, chunk: RawChunk<'a>) -> Self {
        Self {
            schema_version: NATS_SCHEMA_VERSION,
            request_id: request_id.into(),
            chunk,
        }
    }

    pub fn into_owned(self) -> ReasoningRequest<'static> {
        ReasoningRequest {
            schema_version: self.schema_version,
            request_id: Cow::Owned(self.request_id.into_owned()),
            chunk: self.chunk.into_owned(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ReasoningCompleted<'a> {
    pub schema_version: u16,
    #[serde(borrow)]
    pub request_id: Cow<'a, str>,
    #[serde(borrow)]
    pub chunk: EnrichedChunk<'a>,
}

impl<'a> ReasoningCompleted<'a> {
    pub fn new(request_id: impl Into<Cow<'a, str>>, chunk: EnrichedChunk<'a>) -> Self {
        Self {
            schema_version: NATS_SCHEMA_VERSION,
            request_id: request_id.into(),
            chunk,
        }
    }

    pub fn into_owned(self) -> ReasoningCompleted<'static> {
        ReasoningCompleted {
            schema_version: self.schema_version,
            request_id: Cow::Owned(self.request_id.into_owned()),
            chunk: self.chunk.into_owned(),
        }
    }
}

pub fn parse_raw_chunk(input: &[u8]) -> serde_json::Result<RawChunk<'_>> {
    serde_json::from_slice(input)
}

pub fn parse_reasoning_request(input: &[u8]) -> serde_json::Result<ReasoningRequest<'_>> {
    serde_json::from_slice(input)
}

pub fn parse_reasoning_completed(input: &[u8]) -> serde_json::Result<ReasoningCompleted<'_>> {
    serde_json::from_slice(input)
}

#[derive(Debug, Clone, Deserialize)]
pub struct DoclingChunk<'a> {
    #[serde(borrow, alias = "id")]
    pub chunk_id: Cow<'a, str>,
    #[serde(borrow, default)]
    pub document_id: Cow<'a, str>,
    #[serde(borrow, default)]
    pub path: Cow<'a, str>,
    #[serde(borrow, default)]
    pub heading: Cow<'a, str>,
    #[serde(borrow, alias = "content")]
    pub text: Cow<'a, str>,
    #[serde(default)]
    pub page_start: Option<u32>,
    #[serde(default)]
    pub page_end: Option<u32>,
    #[serde(default)]
    pub line_start: Option<u32>,
    #[serde(default)]
    pub line_end: Option<u32>,
}

pub fn parse_docling_chunk(input: &[u8]) -> serde_json::Result<DoclingChunk<'_>> {
    serde_json::from_slice(input)
}

#[derive(Debug, Serialize)]
pub struct LlamaEmbeddingRequest<'a> {
    pub model: Cow<'a, str>,
    #[serde(borrow)]
    pub input: &'a [Cow<'a, str>],
}

#[derive(Debug, Deserialize)]
pub struct LlamaEmbeddingResponse<'a> {
    #[serde(borrow, default)]
    pub object: Cow<'a, str>,
    #[serde(borrow, default)]
    pub model: Cow<'a, str>,
    #[serde(default)]
    pub data: Vec<LlamaEmbeddingData>,
    #[serde(default)]
    pub usage: Option<LlamaEmbeddingUsage>,
}

#[derive(Debug, Deserialize)]
pub struct LlamaEmbeddingData {
    pub index: usize,
    pub embedding: Vec<f32>,
}

#[derive(Debug, Deserialize)]
pub struct LlamaEmbeddingUsage {
    #[serde(default)]
    pub prompt_tokens: u64,
    #[serde(default)]
    pub total_tokens: u64,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
pub struct StringRef {
    pub offset: u32,
    pub length: u32,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DocChunkRecord {
    pub chunk_id: u64,
    pub text_ref: StringRef,
    pub embedding: Vec<f32>,
}

impl DocChunkRecord {
    pub fn new(chunk_id: u64, text_ref: StringRef, embedding: Vec<f32>) -> Result<Self> {
        ensure!(
            embedding.len() == EMBEDDING_DIMENSIONS,
            "chunk {chunk_id} embedding dimension mismatch: got {}, expected {EMBEDDING_DIMENSIONS}",
            embedding.len()
        );
        Ok(Self {
            chunk_id,
            text_ref,
            embedding,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn docling_chunk_borrows_plain_json_strings() -> anyhow::Result<()> {
        let input = br#"{"id":"chunk-7","path":"manual.pdf","heading":"Install","text":"plain borrowed text"}"#;
        let chunk = parse_docling_chunk(input)?;

        assert!(matches!(chunk.chunk_id, Cow::Borrowed("chunk-7")));
        assert!(matches!(chunk.path, Cow::Borrowed("manual.pdf")));
        assert!(matches!(chunk.text, Cow::Borrowed("plain borrowed text")));
        Ok(())
    }

    #[test]
    fn docling_chunk_allocates_only_for_escaped_strings() -> anyhow::Result<()> {
        let input = br#"{"id":"chunk-8","path":"manual.pdf","text":"line one\nline two"}"#;
        let chunk = parse_docling_chunk(input)?;

        assert!(matches!(chunk.path, Cow::Borrowed("manual.pdf")));
        assert!(matches!(chunk.text, Cow::Owned(_)));
        assert_eq!(chunk.text, "line one\nline two");
        Ok(())
    }

    #[test]
    fn doc_chunk_record_rejects_wrong_dimensions() {
        let record = DocChunkRecord::new(
            1,
            StringRef {
                offset: 0,
                length: 4,
            },
            vec![0.0; 32],
        );
        assert!(record.is_err());
    }

    #[test]
    fn raw_chunk_borrows_network_payload_strings() -> anyhow::Result<()> {
        let input = br#"{"id":"chunk-9","document_id":"doc-1","path":"README.md","type":"documentation","text":"borrow me"}"#;
        let chunk = parse_raw_chunk(input)?;

        assert!(matches!(chunk.chunk_id, Cow::Borrowed("chunk-9")));
        assert!(matches!(chunk.path, Cow::Borrowed("README.md")));
        assert!(matches!(chunk.text, Cow::Borrowed("borrow me")));
        assert!(!chunk.is_source_code());
        Ok(())
    }

    #[test]
    fn enriched_chunk_rejects_wrong_dimensions() -> anyhow::Result<()> {
        let raw = RawChunk {
            chunk_id: Cow::Borrowed("source-1"),
            document_id: Cow::Borrowed("doc"),
            path: Cow::Borrowed("src/lib.rs"),
            heading: Cow::Borrowed(""),
            language: Cow::Borrowed("rust"),
            chunk_type: ChunkKind::SourceCode,
            text: Cow::Borrowed("fn main() {}"),
        };

        let result = EnrichedChunk::new(raw, Cow::Borrowed(""), vec![0.0; 8]);

        assert!(result.is_err());
        Ok(())
    }
}
