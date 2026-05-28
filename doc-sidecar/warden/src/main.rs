use std::{borrow::Cow, time::Duration};

use anyhow::Result;
use clap::{Parser, ValueEnum};
use graphkv_warden::{
    egress::DEFAULT_GRAPHKV_DOC_WS_URL,
    embedder::{EmbeddingConfig, DEFAULT_LLAMA_EMBEDDING_ENDPOINT},
    models::DEFAULT_NATS_URL,
    reasoner::{
        run_reasoner, ReasonerConfig, DEFAULT_CONTEXT_PREFIX_WORDS, DEFAULT_REASONER_BATCH_SIZE,
        DEFAULT_REASONER_BATCH_TIMEOUT, DEFAULT_REASONER_REQUEST_TIMEOUT,
        DEFAULT_VLLM_COMPLETIONS_ENDPOINT, DEFAULT_VLLM_MODEL,
    },
    router::{run_router, RouterConfig},
};
use tokio::sync::mpsc;

#[derive(Debug, Clone, Copy, ValueEnum)]
enum Mode {
    Router,
    Reasoner,
}

#[derive(Debug, Parser)]
#[command(name = "graphkv-warden", about = "GraphKV Warden daemon fleet")]
struct Cli {
    #[arg(long, value_enum)]
    mode: Mode,
    #[arg(long, default_value = DEFAULT_NATS_URL)]
    nats_url: String,
    #[arg(long, default_value = DEFAULT_GRAPHKV_DOC_WS_URL)]
    graphkv_ws_url: String,
    #[arg(long, default_value = DEFAULT_LLAMA_EMBEDDING_ENDPOINT)]
    embedding_endpoint: String,
    #[arg(long, default_value = "local-embedder")]
    embedding_model: String,
    #[arg(long, default_value = DEFAULT_VLLM_COMPLETIONS_ENDPOINT)]
    vllm_endpoint: String,
    #[arg(long, default_value = DEFAULT_VLLM_MODEL)]
    vllm_model: String,
    #[arg(long, default_value_t = DEFAULT_REASONER_BATCH_SIZE)]
    batch_size: usize,
    #[arg(long, default_value_t = DEFAULT_REASONER_BATCH_TIMEOUT.as_millis() as u64)]
    batch_timeout_ms: u64,
    #[arg(long, default_value_t = DEFAULT_REASONER_REQUEST_TIMEOUT.as_secs())]
    request_timeout_secs: u64,
    #[arg(long, default_value_t = DEFAULT_CONTEXT_PREFIX_WORDS)]
    prefix_words: u32,
    #[arg(long, default_value_t = 1024)]
    router_buffer: usize,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let request_timeout = Duration::from_secs(cli.request_timeout_secs);
    let embedding = EmbeddingConfig {
        endpoint: Cow::Owned(cli.embedding_endpoint),
        model: Cow::Owned(cli.embedding_model),
        timeout: request_timeout,
    };

    match cli.mode {
        Mode::Router => {
            let (_mock_sender, receiver) = mpsc::channel(cli.router_buffer);
            run_router(
                receiver,
                RouterConfig {
                    nats_url: Cow::Owned(cli.nats_url),
                    websocket_url: Cow::Owned(cli.graphkv_ws_url),
                    embedding,
                },
            )
            .await
        }
        Mode::Reasoner => {
            run_reasoner(ReasonerConfig {
                nats_url: Cow::Owned(cli.nats_url),
                completion_endpoint: Cow::Owned(cli.vllm_endpoint),
                completion_model: Cow::Owned(cli.vllm_model),
                embedding,
                batch_size: cli.batch_size,
                batch_timeout: Duration::from_millis(cli.batch_timeout_ms),
                request_timeout,
                prefix_words: cli.prefix_words,
            })
            .await
        }
    }
}
