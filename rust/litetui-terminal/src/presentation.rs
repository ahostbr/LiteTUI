//! Effective device presentation preferences (transcript, dialogs, footer) as owned by
//! Python settings, plus the pure formatting they drive. The runtime delivers the effective
//! values; nothing here persists or decides a value.
use crate::footer::{FieldKey, FooterField};
use crossterm::event::{KeyCode, KeyModifiers};
use serde_json::{Map, Value};
use std::{
    io,
    time::{Duration, Instant},
};

/// Source `settings.FOOTER_ORDER_DEFAULT`; the editor and renderer share one vocabulary.
pub const FOOTER_ORDER_DEFAULT: [&str; 13] = [
    "pct",
    "ctx",
    "seat",
    "convo",
    "model",
    "think",
    "cache",
    "tps",
    "telemetry",
    "bg",
    "agents",
    "authority",
    "plan",
];
pub const FIELDS: [&str; 19] = [
    "show_thinking",
    "show_stop_line",
    "show_stop_time",
    "autoscroll",
    "error_message_style",
    "dialog_style",
    "dialog_side",
    "footer_show_seat",
    "footer_show_thinking",
    "footer_show_bg",
    "footer_show_subagents",
    "footer_show_convo",
    "footer_show_context",
    "footer_show_context_pct",
    "footer_show_tps",
    "footer_show_cache",
    "footer_task_manager",
    "footer_show_key_hints",
    "footer_order",
];

fn bad(key: &str) -> io::Error {
    io::Error::other(format!("Invalid presentation preference {key}"))
}
pub(crate) fn valid_order(items: &[Value]) -> bool {
    items.len() == FOOTER_ORDER_DEFAULT.len()
        && FOOTER_ORDER_DEFAULT.iter().all(|id| {
            items
                .iter()
                .filter(|item| item.as_str() == Some(id))
                .count()
                == 1
        })
}

/// Defaults are the source defaults, so a runtime that reports none behaves like a fresh install.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Presentation {
    pub show_thinking: bool,
    pub show_stop_line: bool,
    pub show_stop_time: bool,
    pub autoscroll: bool,
    pub sidebar_dialogs: bool,
    pub dialogs_left: bool,
    pub footer_seat: bool,
    pub footer_thinking: bool,
    pub footer_background: bool,
    pub footer_agents: bool,
    pub footer_conversation: bool,
    pub footer_context: bool,
    pub footer_percent: bool,
    pub footer_throughput: bool,
    pub footer_cache: bool,
    pub key_hints: bool,
    /// footer_task_manager: Python's "meters:on/off" toggle on the second footer row.
    pub task_manager: bool,
    /// The dictation shortcut (`stt_hotkey`). It rides the same effective-settings delivery
    /// as the presentation values; a spelling this frontend cannot read binds nothing, as the
    /// source's runtime bind ignores a malformed key.
    pub dictation_hotkey: Option<Hotkey>,
    order: Vec<&'static str>,
}
impl Default for Presentation {
    fn default() -> Self {
        Self {
            show_thinking: true,
            show_stop_line: true,
            show_stop_time: false,
            autoscroll: true,
            sidebar_dialogs: false,
            dialogs_left: false,
            footer_seat: true,
            footer_thinking: true,
            footer_background: true,
            footer_agents: true,
            footer_conversation: true,
            footer_context: true,
            footer_percent: true,
            footer_throughput: true,
            footer_cache: true,
            key_hints: false,
            task_manager: false,
            dictation_hotkey: Hotkey::parse("ctrl+space"),
            order: FOOTER_ORDER_DEFAULT.to_vec(),
        }
    }
}
impl Presentation {
    /// Parse the `settings.effective` object of the source state. Absent keys keep their
    /// default; a present key of the wrong type or vocabulary is a protocol error.
    pub fn from_effective(effective: &Map<String, Value>) -> io::Result<Self> {
        let mut out = Self::default();
        let flag = |key: &str, slot: &mut bool| -> io::Result<()> {
            match effective.get(key) {
                None => Ok(()),
                Some(Value::Bool(value)) => {
                    *slot = *value;
                    Ok(())
                }
                Some(_) => Err(bad(key)),
            }
        };
        let choice = |key: &str, off: &str, on: &str| -> io::Result<Option<bool>> {
            match effective.get(key) {
                None => Ok(None),
                Some(Value::String(value)) if value == off => Ok(Some(false)),
                Some(Value::String(value)) if value == on => Ok(Some(true)),
                Some(_) => Err(bad(key)),
            }
        };
        flag("show_thinking", &mut out.show_thinking)?;
        flag("show_stop_line", &mut out.show_stop_line)?;
        flag("show_stop_time", &mut out.show_stop_time)?;
        flag("autoscroll", &mut out.autoscroll)?;
        flag("footer_show_seat", &mut out.footer_seat)?;
        flag("footer_show_thinking", &mut out.footer_thinking)?;
        flag("footer_show_bg", &mut out.footer_background)?;
        flag("footer_show_subagents", &mut out.footer_agents)?;
        flag("footer_show_convo", &mut out.footer_conversation)?;
        flag("footer_show_context", &mut out.footer_context)?;
        flag("footer_show_context_pct", &mut out.footer_percent)?;
        flag("footer_show_tps", &mut out.footer_throughput)?;
        flag("footer_show_cache", &mut out.footer_cache)?;
        flag("footer_show_key_hints", &mut out.key_hints)?;
        flag("footer_task_manager", &mut out.task_manager)?;
        // Plain/detail wording is applied by the source before text reaches this frontend.
        choice("error_message_style", "plain", "detail")?;
        if let Some(value) = choice("dialog_style", "modal", "sidebar")? {
            out.sidebar_dialogs = value;
        }
        if let Some(value) = choice("dialog_side", "right", "left")? {
            out.dialogs_left = value;
        }
        match effective.get("footer_order") {
            None => {}
            Some(Value::Array(items)) if valid_order(items) => {
                out.order = items
                    .iter()
                    .filter_map(|item| {
                        FOOTER_ORDER_DEFAULT
                            .iter()
                            .find(|id| item.as_str() == Some(id))
                            .copied()
                    })
                    .collect();
            }
            Some(_) => return Err(bad("footer_order")),
        }
        match effective.get("stt_hotkey") {
            None => {}
            Some(Value::String(spelling)) => {
                out.dictation_hotkey = Hotkey::parse(if spelling.trim().is_empty() {
                    "ctrl+space"
                } else {
                    spelling
                })
            }
            Some(_) => return Err(bad("stt_hotkey")),
        }
        Ok(out)
    }
    pub fn order(&self) -> &[&'static str] {
        &self.order
    }
    fn visible(&self, key: FieldKey) -> bool {
        match key {
            FieldKey::Seat => self.footer_seat,
            FieldKey::Thinking => self.footer_thinking,
            FieldKey::Background => self.footer_background,
            FieldKey::Agents => self.footer_agents,
            FieldKey::Conversation => self.footer_conversation,
            FieldKey::Context => self.footer_context,
            FieldKey::Percent => self.footer_percent,
            FieldKey::Throughput => self.footer_throughput,
            FieldKey::Cache => self.footer_cache,
            // The model, authority and plan are never hideable in the source, and held input
            // is a frontend-only safety fact.
            FieldKey::Model | FieldKey::Authority | FieldKey::Plan | FieldKey::RestoredInput => {
                true
            }
        }
    }
    fn footer_id(key: FieldKey) -> &'static str {
        match key {
            FieldKey::Percent => "pct",
            FieldKey::Context => "ctx",
            FieldKey::Seat => "seat",
            // Held input is not a source field: it travels with the conversation id.
            FieldKey::Conversation | FieldKey::RestoredInput => "convo",
            FieldKey::Model => "model",
            FieldKey::Thinking => "think",
            FieldKey::Cache => "cache",
            FieldKey::Throughput => "tps",
            FieldKey::Background => "bg",
            FieldKey::Agents => "agents",
            FieldKey::Authority => "authority",
            FieldKey::Plan => "plan",
        }
    }
    /// Apply the switches and the left-to-right order. Held input stays directly after the
    /// conversation id, wherever the user placed that id.
    pub fn arrange(&self, fields: Vec<FooterField>) -> Vec<FooterField> {
        let rank = |key: FieldKey| {
            let id = Self::footer_id(key);
            let position = self.order.iter().position(|item| *item == id).unwrap_or(0);
            position * 2 + usize::from(key == FieldKey::RestoredInput)
        };
        let mut fields: Vec<_> = fields
            .into_iter()
            .filter(|field| self.visible(field.key))
            .collect();
        fields.sort_by_key(|field| rank(field.key));
        fields
    }
}

/// Wall-clock `(hour, minute)` in the machine's local zone, 24-hour.
#[cfg(windows)]
pub fn local_clock() -> Option<(u8, u8)> {
    use windows_sys::Win32::{Foundation::SYSTEMTIME, System::SystemInformation::GetLocalTime};
    let mut now = SYSTEMTIME {
        wYear: 0,
        wMonth: 0,
        wDayOfWeek: 0,
        wDay: 0,
        wHour: 0,
        wMinute: 0,
        wSecond: 0,
        wMilliseconds: 0,
    };
    // SAFETY: GetLocalTime only writes the caller-owned SYSTEMTIME.
    unsafe { GetLocalTime(&mut now) };
    Some((
        u8::try_from(now.wHour).ok()?,
        u8::try_from(now.wMinute).ok()?,
    ))
}
/// Without a local-zone source the time is omitted rather than shown in the wrong zone.
#[cfg(not(windows))]
pub fn local_clock() -> Option<(u8, u8)> {
    None
}

/// Source `fmt.fmt_dur`.
pub fn duration_text(elapsed: Duration) -> String {
    let seconds = elapsed.as_secs_f64();
    if seconds < 60.0 {
        return format!("{seconds:.1}s");
    }
    let minutes = (seconds / 60.0).floor();
    format!("{}m {:04.1}s", minutes as u64, seconds - minutes * 60.0)
}
fn clock_text((hour, minute): (u8, u8)) -> String {
    let twelve = match hour % 12 {
        0 => 12,
        other => other,
    };
    format!(
        "{twelve}:{minute:02} {}",
        if hour < 12 { "AM" } else { "PM" }
    )
}

/// Frontend-observed tool interval, not provider execution time. Missing requests
/// (including historical/orphan results) stay untimed; the first settlement wins.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ToolTiming {
    started: Option<Instant>,
    settled: Option<Duration>,
}
impl ToolTiming {
    pub(crate) fn started() -> Self {
        Self {
            started: Some(Instant::now()),
            settled: None,
        }
    }
    pub(crate) fn finish(&mut self) {
        self.finish_at(Instant::now());
    }
    fn finish_at(&mut self, now: Instant) {
        if self.settled.is_none() {
            self.settled = self.elapsed_at(now);
        }
    }
    pub fn elapsed_at(&self, now: Instant) -> Option<Duration> {
        self.settled.or_else(|| {
            self.started
                .map(|started| now.saturating_duration_since(started))
        })
    }
    pub fn label(&self) -> String {
        self.elapsed_at(Instant::now()).map_or_else(
            || "duration unknown".into(),
            |elapsed| format!("observed {}", duration_text(elapsed)),
        )
    }
}

/// How one turn ended, for the settled line below its last assistant card.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Ending {
    Completed,
    Stopped,
}
#[derive(Clone, Debug, PartialEq)]
struct Settled {
    elapsed: Duration,
    tps: Option<f64>,
    local: Option<(u8, u8)>,
}
/// Request-to-settlement clock of one turn row. Rows that were never timed here
/// (reconciled history) carry none and draw no line.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TurnTiming {
    started: Option<Instant>,
    settled: Option<Settled>,
}
impl TurnTiming {
    pub fn started() -> Self {
        Self {
            started: Some(Instant::now()),
            settled: None,
        }
    }
    pub fn settle(&mut self, tps: Option<f64>) {
        if let (Some(started), None) = (self.started, &self.settled) {
            self.settled = Some(Settled {
                elapsed: started.elapsed(),
                tps,
                local: local_clock(),
            });
        }
    }
    #[cfg(test)]
    pub(crate) fn fixed(elapsed: Duration, tps: Option<f64>, local: Option<(u8, u8)>) -> Self {
        Self {
            started: None,
            settled: Some(Settled {
                elapsed,
                tps,
                local,
            }),
        }
    }
    /// Source `turnstats.format_turn_stop_line`.
    pub fn line(&self, ending: Ending, presentation: &Presentation) -> Option<String> {
        if !presentation.show_stop_line {
            return None;
        }
        let settled = self.settled.as_ref()?;
        let elapsed = duration_text(settled.elapsed);
        let mut line = match ending {
            Ending::Stopped => format!("stopped after {elapsed}"),
            Ending::Completed => format!("Cooked for {elapsed}"),
        };
        if let Some(rate) = settled.tps {
            line.push_str(&format!(" · {rate:.1} tok/s"));
        }
        if presentation.show_stop_time
            && let Some(local) = settled.local
        {
            line.push_str(&format!(" · done {}", clock_text(local)));
        }
        Some(line)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn effective(extra: Value) -> Map<String, Value> {
        let mut map = Map::new();
        for (key, value) in extra.as_object().unwrap() {
            map.insert(key.clone(), value.clone());
        }
        map
    }

    #[test]
    fn absent_keys_take_source_defaults_and_wrong_types_are_refused() {
        let defaults = Presentation::from_effective(&Map::new()).unwrap();
        assert!(defaults.show_thinking && defaults.show_stop_line && defaults.autoscroll);
        assert!(!defaults.show_stop_time && !defaults.key_hints && !defaults.sidebar_dialogs);
        assert_eq!(defaults.order(), FOOTER_ORDER_DEFAULT);
        for (key, value) in [
            ("show_thinking", json!("false")),
            ("dialog_style", json!("popup")),
            ("dialog_side", json!(1)),
            ("error_message_style", json!("verbose")),
            ("footer_order", json!(["pct"])),
            (
                "footer_order",
                json!([
                    "pct",
                    "pct",
                    "seat",
                    "convo",
                    "model",
                    "think",
                    "cache",
                    "tps",
                    "telemetry",
                    "bg",
                    "agents",
                    "authority",
                    "plan"
                ]),
            ),
        ] {
            assert!(
                Presentation::from_effective(&effective(json!({ key: value }))).is_err(),
                "{key}"
            );
        }
    }

    #[test]
    fn every_footer_switch_hides_exactly_its_field_and_unhideable_fields_stay() {
        let all = [
            (FieldKey::Percent, "footer_show_context_pct"),
            (FieldKey::Context, "footer_show_context"),
            (FieldKey::Seat, "footer_show_seat"),
            (FieldKey::Conversation, "footer_show_convo"),
            (FieldKey::Thinking, "footer_show_thinking"),
            (FieldKey::Cache, "footer_show_cache"),
            (FieldKey::Throughput, "footer_show_tps"),
            (FieldKey::Background, "footer_show_bg"),
            (FieldKey::Agents, "footer_show_subagents"),
        ];
        let fields = || -> Vec<FooterField> {
            all.iter()
                .map(|(key, _)| *key)
                .chain([
                    FieldKey::Model,
                    FieldKey::RestoredInput,
                    FieldKey::Authority,
                    FieldKey::Plan,
                ])
                .map(|key| FooterField::new(key, format!("{key:?}")))
                .collect()
        };
        assert_eq!(
            Presentation::default().arrange(fields()).len(),
            all.len() + 4
        );
        for (key, switch) in all {
            let hidden =
                Presentation::from_effective(&effective(json!({ switch: false }))).unwrap();
            let kept: Vec<_> = hidden.arrange(fields()).iter().map(|f| f.key).collect();
            assert!(!kept.contains(&key), "{switch}");
            assert_eq!(kept.len(), all.len() + 3, "{switch}");
        }
    }

    #[test]
    fn order_places_fields_and_held_input_follows_the_conversation_id() {
        let mut order: Vec<_> = FOOTER_ORDER_DEFAULT.iter().map(|id| json!(id)).collect();
        let tps = order.remove(7);
        order.insert(0, tps);
        let convo = order.remove(4);
        order.push(convo);
        let presentation =
            Presentation::from_effective(&effective(json!({ "footer_order": order }))).unwrap();
        let keys: Vec<_> = presentation
            .arrange(vec![
                FooterField::new(FieldKey::Percent, "1%"),
                FooterField::new(FieldKey::Context, "ctx"),
                FooterField::new(FieldKey::Conversation, "c"),
                FooterField::new(FieldKey::RestoredInput, "held:1"),
                FooterField::new(FieldKey::Model, "m"),
                FooterField::new(FieldKey::Throughput, "t"),
            ])
            .iter()
            .map(|f| f.key)
            .collect();
        assert_eq!(
            keys,
            [
                FieldKey::Throughput,
                FieldKey::Percent,
                FieldKey::Context,
                FieldKey::Model,
                FieldKey::Conversation,
                FieldKey::RestoredInput,
            ]
        );
    }

    #[test]
    fn stop_line_follows_source_format_and_switches() {
        let timing = TurnTiming::fixed(Duration::from_secs_f64(75.25), Some(38.24), Some((0, 5)));
        let plain = Presentation::default();
        assert_eq!(
            timing.line(Ending::Completed, &plain).unwrap(),
            "Cooked for 1m 15.2s · 38.2 tok/s"
        );
        assert_eq!(
            timing.line(Ending::Stopped, &plain).unwrap(),
            "stopped after 1m 15.2s · 38.2 tok/s"
        );
        let timed =
            Presentation::from_effective(&effective(json!({"show_stop_time": true}))).unwrap();
        assert!(
            timing
                .line(Ending::Completed, &timed)
                .unwrap()
                .ends_with(" · done 12:05 AM")
        );
        let hidden =
            Presentation::from_effective(&effective(json!({"show_stop_line": false}))).unwrap();
        assert_eq!(timing.line(Ending::Completed, &hidden), None);
        assert_eq!(clock_text((13, 7)), "1:07 PM");
        assert_eq!(duration_text(Duration::from_millis(400)), "0.4s");
        assert_eq!(TurnTiming::default().line(Ending::Completed, &plain), None);
    }
}

/// A key chord in Textual's spelling (`ctrl+space`, `alt+r`, `f9`, `ctrl+shift+d`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hotkey {
    code: KeyCode,
    modifiers: KeyModifiers,
}
impl Hotkey {
    pub fn parse(spelling: &str) -> Option<Self> {
        let spelling = spelling.trim().to_ascii_lowercase();
        let mut parts: Vec<&str> = spelling.split('+').collect();
        // `ctrl++` names the plus key itself.
        let key = if spelling.ends_with("++") {
            parts.truncate(parts.len().saturating_sub(2));
            "+"
        } else {
            parts.pop()?
        };
        let mut modifiers = KeyModifiers::NONE;
        for part in parts {
            modifiers |= match part {
                "ctrl" | "control" => KeyModifiers::CONTROL,
                "shift" => KeyModifiers::SHIFT,
                "alt" | "meta" => KeyModifiers::ALT,
                _ => return None,
            };
        }
        let code = match key {
            "space" => KeyCode::Char(' '),
            "enter" => KeyCode::Enter,
            "tab" => KeyCode::Tab,
            "escape" => KeyCode::Esc,
            "backspace" => KeyCode::Backspace,
            "delete" => KeyCode::Delete,
            "insert" => KeyCode::Insert,
            "home" => KeyCode::Home,
            "end" => KeyCode::End,
            "pageup" => KeyCode::PageUp,
            "pagedown" => KeyCode::PageDown,
            "up" => KeyCode::Up,
            "down" => KeyCode::Down,
            "left" => KeyCode::Left,
            "right" => KeyCode::Right,
            _ if key.len() > 1 && key.starts_with('f') => {
                KeyCode::F(key[1..].parse().ok().filter(|n| (1..=24).contains(n))?)
            }
            _ => {
                let mut chars = key.chars();
                let c = chars.next()?;
                if chars.next().is_some() || c.is_control() {
                    return None;
                }
                KeyCode::Char(c)
            }
        };
        Some(Self { code, modifiers })
    }
    pub fn matches(&self, code: KeyCode, modifiers: KeyModifiers) -> bool {
        // Ctrl+Space reaches a terminal without the kitty protocol as NUL (`ctrl+@`).
        let space = self.code == KeyCode::Char(' ') && self.modifiers == KeyModifiers::CONTROL;
        if space && modifiers == KeyModifiers::CONTROL && matches!(code, KeyCode::Char(' ' | '@')) {
            return true;
        }
        let code = match code {
            KeyCode::Char(c) => KeyCode::Char(c.to_ascii_lowercase()),
            other => other,
        };
        code == self.code && modifiers == self.modifiers
    }
}

#[cfg(test)]
mod hotkey_tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn default_ctrl_space_matches_the_nul_spelling_terminals_deliver() {
        let hotkey = Presentation::default().dictation_hotkey.unwrap();
        assert!(hotkey.matches(KeyCode::Char(' '), KeyModifiers::CONTROL));
        assert!(hotkey.matches(KeyCode::Char('@'), KeyModifiers::CONTROL));
        assert!(!hotkey.matches(KeyCode::Char(' '), KeyModifiers::NONE));
    }

    #[test]
    fn configured_spellings_parse_and_malformed_ones_bind_nothing() {
        let read = |value: Value| {
            let effective = json!({ "stt_hotkey": value });
            Presentation::from_effective(effective.as_object().unwrap())
        };
        let alt_r = read(json!("alt+r")).unwrap().dictation_hotkey.unwrap();
        assert!(alt_r.matches(KeyCode::Char('r'), KeyModifiers::ALT));
        assert!(!alt_r.matches(KeyCode::Char('r'), KeyModifiers::CONTROL));
        let f9 = read(json!("f9")).unwrap().dictation_hotkey.unwrap();
        assert!(f9.matches(KeyCode::F(9), KeyModifiers::NONE));
        assert_eq!(read(json!("hyper+q")).unwrap().dictation_hotkey, None);
        assert_eq!(read(json!("ctrl+")).unwrap().dictation_hotkey, None);
        assert!(read(json!("")).unwrap().dictation_hotkey.is_some());
        assert!(read(json!(7)).is_err());
    }
}

#[cfg(test)]
mod tool_timing_tests {
    use super::*;

    #[test]
    fn tool_clock_advances_then_freezes_at_first_settlement_without_inventing_a_start() {
        let start = Instant::now();
        let mut timing = ToolTiming {
            started: Some(start),
            settled: None,
        };
        assert_eq!(
            timing.elapsed_at(start + Duration::from_millis(400)),
            Some(Duration::from_millis(400))
        );
        timing.finish_at(start + Duration::from_millis(62500));
        assert_eq!(timing.label(), "observed 1m 02.5s");
        timing.finish_at(start + Duration::from_secs(100));
        assert_eq!(
            timing.elapsed_at(start + Duration::from_secs(200)),
            Some(Duration::from_millis(62500))
        );
        let mut unknown = ToolTiming::default();
        unknown.finish_at(start);
        assert_eq!(unknown.elapsed_at(start + Duration::from_secs(200)), None);
        assert_eq!(unknown.label(), "duration unknown");
    }
}
