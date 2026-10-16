//! Bounded raw JSONL ingress, not a process, async reader or session owner.
//!
//! Queue limits count original wire bytes, not decoded heap size. A caller must
//! drain the accepted prefix before reporting terminal transport uncertainty to
//! its decoder/client/projection. Transport acceptance is NOT decoder validity:
//! on decode failure, stop protocol delivery; remaining raw frames are cleanup
//! only, never replayed through a replacement decoder. This module never retries
//! or resets a stream. Feed/drain scheduling can affect overload; there is no
//! async backpressure or session-level admission gate here.
use std::collections::VecDeque;

#[derive(Clone, Copy, Debug)]
pub struct IngressLimits {
    /// Per-frame bytes excluding LF, including CR (same as JsonlDecoder).
    pub frame_bytes: usize,
    pub queued_frames: usize,
    /// Aggregate queued original bytes INCLUDING each terminating LF.
    pub queued_bytes: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IngressError {
    InvalidLimit,
    FrameTooLarge { limit: usize },
    FrameQueueFull { limit: usize },
    ByteQueueFull { limit: usize },
    TruncatedFrame,
    Aborted,
    Closed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IngressStatus {
    Open,
    Eof,
    Fault(IngressError),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Push {
    Accepted,
    StaleGeneration,
}

/// A single-generation FIFO, with a separately bounded partial frame.
///
/// At most `queued_bytes` complete-frame content bytes and `frame_bytes`
/// partial bytes are retained. Counts exclude Vec capacity, object overhead,
/// borrowed input chunks and frames transferred to the caller by `pop_frame`.
/// No caller-supplied production defaults, parsing or event selection is hidden
/// here. A full queue is a permanent loss-of-certainty fault, not backpressure:
/// the offending frame and unprocessed suffix are discarded explicitly; the
/// accepted prefix remains drainable. Never retry a failed chunk on this or a
/// replacement connection. A new connection requires a new caller generation.
pub struct Inbound {
    generation: u64,
    limits: IngressLimits,
    pending: Vec<u8>,
    frames: VecDeque<Vec<u8>>,
    queued_bytes: usize,
    status: IngressStatus,
}

impl Inbound {
    pub fn new(generation: u64, limits: IngressLimits) -> Result<Self, IngressError> {
        if limits.frame_bytes == 0 || limits.queued_frames == 0 || limits.queued_bytes == 0 {
            return Err(IngressError::InvalidLimit);
        }
        Ok(Self {
            generation,
            limits,
            pending: Vec::new(),
            frames: VecDeque::new(),
            queued_bytes: 0,
            status: IngressStatus::Open,
        })
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn status(&self) -> &IngressStatus {
        &self.status
    }
    pub fn queued_frames(&self) -> usize {
        self.frames.len()
    }
    pub fn queued_bytes(&self) -> usize {
        self.queued_bytes
    }
    pub fn partial_bytes(&self) -> usize {
        self.pending.len()
    }

    fn open(&self) -> Result<(), IngressError> {
        match &self.status {
            IngressStatus::Open => Ok(()),
            IngressStatus::Eof => Err(IngressError::Closed),
            IngressStatus::Fault(error) => Err(error.clone()),
        }
    }
    fn fail(&mut self, error: IngressError) -> IngressError {
        self.pending.clear();
        self.status = IngressStatus::Fault(error.clone());
        error
    }

    /// Feed borrowed bytes once. On error, accepted earlier frames are retained,
    /// but the offending frame and remaining input are NOT recoverable/retryable.
    /// Stale generations are inert even after EOF/fault.
    pub fn push(&mut self, generation: u64, input: &[u8]) -> Result<Push, IngressError> {
        if generation != self.generation {
            return Ok(Push::StaleGeneration);
        }
        self.open()?;
        for &byte in input {
            if byte != b'\n' {
                if self.pending.len() == self.limits.frame_bytes {
                    return Err(self.fail(IngressError::FrameTooLarge {
                        limit: self.limits.frame_bytes,
                    }));
                }
                self.pending.push(byte);
                continue;
            }
            // Same ASCII-only complete blank-line convention as JsonlDecoder.
            // Oversized blank lines still fault at the per-frame bound above.
            if self.pending.iter().all(u8::is_ascii_whitespace) {
                self.pending.clear();
                continue;
            }
            if self.frames.len() == self.limits.queued_frames {
                return Err(self.fail(IngressError::FrameQueueFull {
                    limit: self.limits.queued_frames,
                }));
            }
            let Some(bytes) = self.pending.len().checked_add(1) else {
                return Err(self.fail(IngressError::ByteQueueFull {
                    limit: self.limits.queued_bytes,
                }));
            };
            if bytes > self.limits.queued_bytes.saturating_sub(self.queued_bytes) {
                return Err(self.fail(IngressError::ByteQueueFull {
                    limit: self.limits.queued_bytes,
                }));
            }
            self.pending.push(b'\n');
            self.frames.push_back(std::mem::take(&mut self.pending));
            self.queued_bytes += bytes;
        }
        Ok(Push::Accepted)
    }

    /// Record EOF, not successful work or descendant cleanup. An unterminated
    /// fragment (including whitespace) is a fault, as in JsonlDecoder::finish.
    pub fn finish(&mut self, generation: u64) -> Result<Push, IngressError> {
        if generation != self.generation {
            return Ok(Push::StaleGeneration);
        }
        self.open()?;
        if !self.pending.is_empty() {
            return Err(self.fail(IngressError::TruncatedFrame));
        }
        self.status = IngressStatus::Eof;
        Ok(Push::Accepted)
    }

    /// Abandon open ingress, clearing partial bytes but preserving the FIFO for
    /// the owner's explicit cleanup. Existing EOF/fault reasons are not replaced.
    /// This is local transport abandonment, NOT a provider/tool cancel command.
    pub fn abort(&mut self, generation: u64) -> Push {
        if generation != self.generation {
            return Push::StaleGeneration;
        }
        if self.status == IngressStatus::Open {
            self.fail(IngressError::Aborted);
        }
        Push::Accepted
    }

    /// Transfer one complete original frame, including LF, in FIFO order.
    /// Draining does not clear terminal status or permit new input after a fault.
    /// The caller owns/counts any frames it retains after this transfer.
    pub fn pop_frame(&mut self) -> Option<Vec<u8>> {
        let frame = self.frames.pop_front()?;
        self.queued_bytes -= frame.len();
        Some(frame)
    }
}
