use std::{env, time::Instant};

use anyhow::{Context, Result};
use graphkv_warden::{
    egress::serialize_and_compress,
    models::{DocChunkRecord, StringRef},
    EMBEDDING_DIMENSIONS,
};
use serde::Serialize;

#[derive(Debug, Serialize)]
struct Summary {
    runs: usize,
    mean_ms: f64,
    median_ms: f64,
    p99_ms: f64,
    best_ms: f64,
    worst_ms: f64,
}

#[derive(Debug, Serialize)]
struct ProfileReport {
    records: usize,
    dimensions: usize,
    uncompressed_json_bytes: usize,
    compressed_bytes: usize,
    compression_ratio: f64,
    serialize_compress: Summary,
}

fn percentile(sorted: &[f64], percentile: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let index = ((sorted.len() as f64 * percentile).ceil() as usize).saturating_sub(1);
    sorted[index.min(sorted.len() - 1)]
}

fn summarize(samples: &[f64]) -> Summary {
    let mut sorted = samples.to_vec();
    sorted.sort_by(f64::total_cmp);
    let sum: f64 = sorted.iter().sum();
    Summary {
        runs: sorted.len(),
        mean_ms: if sorted.is_empty() {
            0.0
        } else {
            sum / sorted.len() as f64
        },
        median_ms: percentile(&sorted, 0.50),
        p99_ms: percentile(&sorted, 0.99),
        best_ms: sorted.first().copied().unwrap_or_default(),
        worst_ms: sorted.last().copied().unwrap_or_default(),
    }
}

fn parse_arg(args: &[String], name: &str, default_value: usize) -> Result<usize> {
    let Some(index) = args.iter().position(|value| value == name) else {
        return Ok(default_value);
    };
    let value = args
        .get(index + 1)
        .with_context(|| format!("missing value for {name}"))?;
    value
        .parse::<usize>()
        .with_context(|| format!("invalid numeric value for {name}: {value}"))
}

fn build_records(count: usize) -> Result<Vec<DocChunkRecord>> {
    let mut records = Vec::with_capacity(count);
    for index in 0..count {
        let mut embedding = vec![0.0_f32; EMBEDDING_DIMENSIONS];
        embedding[index % EMBEDDING_DIMENSIONS] = 1.0;
        records.push(DocChunkRecord::new(
            index as u64,
            StringRef {
                offset: (index * 32) as u32,
                length: 32,
            },
            embedding,
        )?);
    }
    Ok(records)
}

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    let records = parse_arg(&args, "--records", 256)?;
    let runs = parse_arg(&args, "--runs", 30)?;
    let payload = build_records(records)?;
    let uncompressed_json_bytes = serde_json::to_vec(&payload)
        .context("failed to serialize profile payload for size accounting")?
        .len();

    let mut samples = Vec::with_capacity(runs);
    let mut compressed_bytes = 0;
    for _ in 0..runs {
        let started = Instant::now();
        let compressed = serialize_and_compress(&payload)?;
        samples.push(started.elapsed().as_secs_f64() * 1000.0);
        compressed_bytes = compressed.len();
        std::hint::black_box(compressed);
    }

    let report = ProfileReport {
        records,
        dimensions: EMBEDDING_DIMENSIONS,
        uncompressed_json_bytes,
        compressed_bytes,
        compression_ratio: if uncompressed_json_bytes == 0 {
            0.0
        } else {
            compressed_bytes as f64 / uncompressed_json_bytes as f64
        },
        serialize_compress: summarize(&samples),
    };
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
