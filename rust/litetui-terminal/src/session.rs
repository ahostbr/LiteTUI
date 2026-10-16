//! Offline, single-generation owner of ingress, decoding, correlation and view.
//! No process, I/O, command writer, authority, retry or provider cancellation.
use crate::{
    protocol::{
        ClientError, ClientFault, CommandId, DecodeError, Delivery, JsonlDecoder, Notice,
        WireClient,
    },
    stream::{Limits as ProjectionLimits, Projection, ProjectionFault},
    transport::{Inbound, IngressError, IngressLimits, IngressStatus, Push},
};
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BuildError {
    Ingress(IngressError),
    Wire(ClientError),
    Projection(ProjectionFault),
}

/// Why protocol delivery ended. Ingress/wire/projection evidence remains
/// separately inspectable: a decode error does not erase a prior overload.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Termination {
    Eof,
    Ingress(IngressError),
    Decode(DecodeError),
    LocalAbort,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    /// Open input with no complete frame available (possibly a partial frame).
    Idle,
    /// A selected wire notice, not a successful projection or completed work.
    Notice {
        notice: Notice,
        projection_fault: Option<ProjectionFault>,
    },
    /// Reported exactly once, AFTER finalization and any accepted-prefix delivery.
    Terminated(Termination),
    Closed,
}

enum Phase {
    Active,
    Finalized { reason: Termination, reported: bool },
}

/// All four components are private so their generation and terminal/admission
/// ordering cannot be changed independently. Limits/policy remain caller-owned.
///
/// EOF/ingress faults block registration immediately but drain accepted frames.
/// Decode failure or explicit local abort instead abandons the queued suffix.
/// Wire/projection faults block registration, but pending responses still drain.
/// Returned notices/frames retained by consumers are outside internal budgets.
pub struct Session {
    generation: u64,
    ingress: Inbound,
    decoder: JsonlDecoder,
    client: WireClient,
    projection: Projection,
    phase: Phase,
}

impl Session {
    pub fn new(
        generation: u64,
        ingress_limits: IngressLimits,
        pending_limit: usize,
        projection_limits: ProjectionLimits,
        policy: impl IntoIterator<Item = (String, Delivery)>,
    ) -> Result<Self, BuildError> {
        let ingress = Inbound::new(generation, ingress_limits).map_err(BuildError::Ingress)?;
        let decoder = JsonlDecoder::new(ingress_limits.frame_bytes)
            .expect("Inbound validated the shared positive frame limit");
        let client =
            WireClient::new(generation, pending_limit, policy).map_err(BuildError::Wire)?;
        let projection =
            Projection::new(generation, projection_limits).map_err(BuildError::Projection)?;
        Ok(Self {
            generation,
            ingress,
            decoder,
            client,
            projection,
            phase: Phase::Active,
        })
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn projection(&self) -> &Projection {
        &self.projection
    }
    pub fn ingress_status(&self) -> &IngressStatus {
        self.ingress.status()
    }
    pub fn wire_fault(&self) -> Option<&ClientFault> {
        self.client.fault()
    }
    pub fn pending_count(&self) -> usize {
        self.client.pending_count()
    }
    pub fn queued_frames(&self) -> usize {
        self.ingress.queued_frames()
    }
    pub fn queued_bytes(&self) -> usize {
        self.ingress.queued_bytes()
    }
    pub fn partial_bytes(&self) -> usize {
        self.ingress.partial_bytes()
    }
    pub fn termination(&self) -> Option<&Termination> {
        match &self.phase {
            Phase::Active => None,
            Phase::Finalized { reason, .. } => Some(reason),
        }
    }
    fn admits_registration(&self) -> bool {
        matches!(self.phase, Phase::Active)
            && self.ingress.status() == &IngressStatus::Open
            && self.client.fault().is_none()
            && self.projection.fault().is_none()
    }
    pub fn ready_for_reads(&self) -> bool {
        self.admits_registration() && self.client.ready_for_reads()
    }
    /// Allocate a future hello command, not a write or authority grant.
    pub fn begin_hello(&mut self) -> Result<Value, ClientError> {
        if !self.admits_registration() {
            return Err(ClientError::NotReady);
        }
        self.client.begin_hello()
    }
    /// Register correlation only. This does not send or authorize a command.
    pub fn register_read(&mut self) -> Result<CommandId, ClientError> {
        if !self.ready_for_reads() {
            return Err(ClientError::NotReady);
        }
        self.client.register_read()
    }
    pub fn push(&mut self, generation: u64, input: &[u8]) -> Result<Push, IngressError> {
        if generation != self.generation {
            return Ok(Push::StaleGeneration);
        }
        if self.termination().is_some() {
            return Err(IngressError::Closed);
        }
        self.ingress.push(generation, input)
    }
    pub fn finish(&mut self, generation: u64) -> Result<Push, IngressError> {
        if generation != self.generation {
            return Ok(Push::StaleGeneration);
        }
        if self.termination().is_some() {
            return Err(IngressError::Closed);
        }
        self.ingress.finish(generation)
    }

    /// Local abandonment, NOT a provider/tool cancellation or a successful stop.
    /// Discards queued prefix/partial data deliberately. A finalized reason never
    /// changes, including when its Terminated update has not yet been polled.
    pub fn abort(&mut self, generation: u64) -> Push {
        if generation != self.generation {
            return Push::StaleGeneration;
        }
        self.finalize(Termination::LocalAbort);
        Push::Accepted
    }

    fn finalize(&mut self, reason: Termination) {
        if !matches!(self.phase, Phase::Active) {
            return;
        }
        // Inbound abort preserves prior EOF/fault evidence. Open partial input
        // is cleared; remaining frames are cleanup-only and never decoded again.
        self.ingress.abort(self.generation);
        while self.ingress.pop_frame().is_some() {}
        self.client.disconnect();
        self.projection.disconnect();
        self.phase = Phase::Finalized {
            reason,
            reported: false,
        };
    }

    /// Deliver at most one original frame. Termination is separate from FIFO
    /// exhaustion; accepted responses/events are processed before ingress EOF or
    /// overflow is finalized. Wire/projection faults do not stop response drain.
    pub fn poll_one(&mut self) -> Update {
        if let Phase::Finalized { reason, reported } = &mut self.phase {
            if *reported {
                return Update::Closed;
            }
            *reported = true;
            return Update::Terminated(reason.clone());
        }
        if let Some(frame) = self.ingress.pop_frame() {
            let mut parsed = None;
            if let Err(error) = self.decoder.push(&frame, |value| parsed = Some(value)) {
                self.finalize(Termination::Decode(error));
                return self.poll_one(); // finalized: emits exactly one terminal update
            }
            let frame = parsed.expect("nonblank complete ingress frame decoded to an object");
            let notice = self.client.ingest(self.generation, frame);
            let projection_fault = self.projection.apply(self.generation, &notice).err();
            return Update::Notice {
                notice,
                projection_fault,
            };
        }
        let reason = match self.ingress.status() {
            IngressStatus::Open => return Update::Idle,
            IngressStatus::Eof => Termination::Eof,
            IngressStatus::Fault(error) => Termination::Ingress(error.clone()),
        };
        // Complete accepted frames may leave the decoder at a clean boundary,
        // but that must not erase the independent ingress failure/EOF evidence.
        let reason = match self.decoder.finish() {
            Ok(()) => reason,
            Err(error) => Termination::Decode(error),
        };
        self.finalize(reason);
        self.poll_one() // finalized; cannot recursively decode another frame
    }
}
