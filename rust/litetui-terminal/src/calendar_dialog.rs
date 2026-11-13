//! `/cal`: Python's CalendarScreen chain (src/litetui/plugins/scheduler_ui.py), drawn natively.
//!
//! Python's /cal opens CalendarScreen (the month grid, the JOBS pane, the hints, Dock to side);
//! a click on a day opens DayScreen (the day's jobs and "new job on this day…"), and a job or
//! `n` there opens JobScreen (the job form). Every decision and every layout fact is Python's:
//! the real screens run headless in the source (native_calendar_dialog.py), laid out at this
//! terminal's size, and each answer carries the top screen's parts with their screen regions.
//! This view draws them there in Textual's own shapes (thick box, tall inputs, 3-row buttons,
//! the scrollbar's eighth-block thumb), keeps the typed edits until the next act, and sends
//! keys, clicks, picks, ticker steps, focus moves and scrolls to Python.
use crate::theme::Theme;
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    widgets::Clear,
};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::io;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

pub const OPERATIONS: [&str; 4] = [
    "gui.calendar.native_dialog_open",
    "gui.calendar.native_dialog_act",
    "gui.calendar.native_dialog_read",
    "gui.calendar.native_dialog_close",
];
/// The same four for /modelcfg (native_modelcfg_dialog.py).
pub const MODELCFG_OPERATIONS: [&str; 4] = [
    "gui.modelcfg.native_dialog_open",
    "gui.modelcfg.native_dialog_act",
    "gui.modelcfg.native_dialog_read",
    "gui.modelcfg.native_dialog_close",
];

/// Which Python dialog this view draws (both are answered as screen regions).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Kind {
    /// /cal: CalendarScreen, DayScreen, JobScreen (scheduler_ui.py).
    #[default]
    Calendar,
    /// /modelcfg: ModelConfigScreen (model_switch.py).
    ModelConfig,
}
impl Kind {
    pub fn operations(self) -> [&'static str; 4] {
        match self {
            Kind::Calendar => OPERATIONS,
            Kind::ModelConfig => MODELCFG_OPERATIONS,
        }
    }
    fn snapshot_kind(self) -> &'static str {
        match self {
            Kind::Calendar => "calendar_dialog",
            Kind::ModelConfig => "modelcfg_dialog",
        }
    }
}
const MAX_ITEMS: usize = 1024;
const MAX_LINES: usize = 400;
/// A tab strip: (pane id, label, region) per tab, and the active pane.
type TabStrip = (Vec<(String, String, Rect)>, Option<String>);
/// The tab strip's place in the focus chain (Textual's Tabs widget has no id here).
const TABS: &str = "@tabs";

/// One styled run: its text and Rich's style string ("bold #ba3c5b", "dim #e0e0e0").
pub type Run = (String, String);
pub type Lines = Vec<Vec<Run>>;

#[derive(Clone, Debug, PartialEq)]
pub struct Opt {
    pub id: Option<String>,
    pub disabled: bool,
    pub lines: Lines,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Control {
    Text {
        text: String,
        classes: Vec<String>,
    },
    Input {
        value: String,
        placeholder: String,
    },
    Select {
        prompt: String,
        options: Vec<String>,
        value: Option<usize>,
    },
    Switch {
        value: bool,
    },
    Button {
        label: String,
        variant: String,
    },
    Options {
        options: Vec<Opt>,
        highlighted: Option<usize>,
    },
    Ticker {
        value: i64,
        text: String,
    },
    Line(Vec<Item>),
    /// A scrolling container: its children are cut to `clip`, with its own scrollbar.
    Clip {
        items: Vec<Item>,
        clip: Rect,
        scroll_y: u16,
        max_scroll_y: u16,
        /// Where Textual drew its vertical scrollbar (its own width), when it shows one.
        bar: Option<Rect>,
    },
    /// A TabbedContent's tab strip: (pane id, label, region) and the active pane.
    Tabs {
        tabs: Vec<(String, String, Rect)>,
        active: Option<String>,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct Item {
    pub id: Option<String>,
    pub region: Rect,
    pub disabled: bool,
    pub control: Control,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Part {
    pub region: Rect,
    pub lines: Lines,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Screen {
    Month {
        grid: Part,
        side: Part,
        hints: Part,
    },
    /// The day popup or the job form: the box's widgets, its content area and its scroll.
    Form {
        job: bool,
        content: Rect,
        scroll_y: u16,
        max_scroll_y: u16,
        focused: Option<String>,
        items: Vec<Item>,
        /// The box's own scrollbar region, when Textual shows one.
        bar: Option<Rect>,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub frame: Rect,
    pub boxed: Rect,
    pub dock: Option<(Rect, String)>,
    pub screen: Screen,
}

fn bad() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "Malformed calendar")
}
fn text(v: &Value, key: &str) -> io::Result<String> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| s.len() <= 64 * 1024)
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn id(v: &Value) -> io::Result<Option<String>> {
    match v.get("id") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if !s.is_empty() && s.len() <= 96 => Ok(Some(s.clone())),
        _ => Err(bad()),
    }
}
fn region(v: &Value) -> io::Result<Rect> {
    let n = v.as_array().filter(|a| a.len() == 4).ok_or_else(bad)?;
    let mut out = [0u16; 4];
    for (slot, value) in out.iter_mut().zip(n) {
        // Scrolled widgets sit above the box's top: clamp what is off the screen to 0 rows.
        let raw = value
            .as_i64()
            .filter(|n| n.abs() <= 10_000)
            .ok_or_else(bad)?;
        *slot = raw.clamp(0, 10_000) as u16;
    }
    let raw_y = n[1].as_i64().unwrap_or(0);
    if raw_y < 0 {
        // Entirely or partly above the screen: shorten it by what is cut off.
        out[3] = out[3].saturating_sub(raw_y.unsigned_abs() as u16);
    }
    Ok(Rect::new(out[0], out[1], out[2], out[3]))
}
pub fn lines(v: &Value) -> io::Result<Lines> {
    let rows = v
        .as_array()
        .filter(|a| a.len() <= MAX_LINES)
        .ok_or_else(bad)?;
    rows.iter()
        .map(|row| {
            row.as_array()
                .filter(|r| r.len() <= 256)
                .ok_or_else(bad)?
                .iter()
                .map(|run| Ok((text(run, "text")?, text(run, "style")?)))
                .collect()
        })
        .collect()
}
fn part(v: &Value) -> io::Result<Part> {
    Ok(Part {
        region: region(&v["region"])?,
        lines: lines(&v["lines"])?,
    })
}
fn item(v: &Value, depth: usize, count: &mut usize) -> io::Result<Item> {
    *count += 1;
    if *count > MAX_ITEMS || depth > 8 {
        return Err(bad());
    }
    let control = match v.get("kind").and_then(Value::as_str) {
        Some("text" | "label") => Control::Text {
            text: text(v, "text")?,
            classes: v
                .get("classes")
                .and_then(Value::as_array)
                .map(|c| {
                    c.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default(),
        },
        Some("input") => Control::Input {
            value: text(v, "value")?,
            placeholder: text(v, "placeholder")?,
        },
        Some("select") => {
            let options = v
                .get("options")
                .and_then(Value::as_array)
                .filter(|o| o.len() <= 512)
                .ok_or_else(bad)?
                .iter()
                .map(|o| o.as_str().map(str::to_owned).ok_or_else(bad))
                .collect::<io::Result<Vec<_>>>()?;
            let value = match text(v, "value")?.as_str() {
                "" => None,
                n => Some(n.parse::<usize>().map_err(|_| bad())?).filter(|i| *i < options.len()),
            };
            Control::Select {
                prompt: text(v, "prompt")?,
                options,
                value,
            }
        }
        Some("switch") => Control::Switch {
            value: v["value"].as_bool().ok_or_else(bad)?,
        },
        Some("button") => Control::Button {
            label: text(v, "label")?,
            variant: text(v, "variant")?,
        },
        Some("options") => Control::Options {
            options: v["options"]
                .as_array()
                .filter(|o| o.len() <= 512)
                .ok_or_else(bad)?
                .iter()
                .map(|o| {
                    Ok(Opt {
                        id: id(o)?,
                        disabled: o["disabled"].as_bool().ok_or_else(bad)?,
                        lines: lines(&o["lines"])?,
                    })
                })
                .collect::<io::Result<Vec<_>>>()?,
            highlighted: v["highlighted"].as_u64().map(|n| n as usize),
        },
        Some("ticker") => Control::Ticker {
            value: v["value"].as_i64().ok_or_else(bad)?,
            text: text(v, "text")?,
        },
        Some("line") => {
            let items = v["items"]
                .as_array()
                .ok_or_else(bad)?
                .iter()
                .map(|i| item(i, depth + 1, count))
                .collect::<io::Result<Vec<_>>>()?;
            if v.get("clip").is_some() {
                Control::Clip {
                    items,
                    clip: region(&v["clip"])?,
                    scroll_y: v["scroll_y"].as_u64().ok_or_else(bad)? as u16,
                    max_scroll_y: v["max_scroll_y"].as_u64().ok_or_else(bad)? as u16,
                    bar: match v.get("bar") {
                        None | Some(Value::Null) => None,
                        Some(b) => Some(region(b)?),
                    },
                }
            } else {
                Control::Line(items)
            }
        }
        Some("tabs") => Control::Tabs {
            tabs: v["tabs"]
                .as_array()
                .filter(|t| t.len() <= 32)
                .ok_or_else(bad)?
                .iter()
                .map(|t| {
                    let id = text(t, "id")?;
                    let pane = id.strip_prefix("--content-tab-").unwrap_or(&id).to_owned();
                    Ok((pane, text(t, "label")?, region(&t["region"])?))
                })
                .collect::<io::Result<Vec<_>>>()?,
            active: v["active"]
                .as_str()
                .map(|a| a.strip_prefix("--content-tab-").unwrap_or(a).to_owned()),
        },
        _ => return Err(bad()),
    };
    Ok(Item {
        id: id(v)?,
        region: region(&v["region"])?,
        disabled: v.get("disabled").and_then(Value::as_bool).unwrap_or(false),
        control,
    })
}
impl Snapshot {
    /// The top screen, or None when the chain has closed (the month's own q or Escape).
    pub fn parse(v: &Value) -> io::Result<Option<Self>> {
        Self::parse_kind(v, Kind::Calendar)
    }
    pub fn parse_kind(v: &Value, kind: Kind) -> io::Result<Option<Self>> {
        if v["version"] != 1 || v["kind"] != kind.snapshot_kind() {
            return Err(bad());
        }
        let screen = match v["screen"].as_str() {
            Some("closed") => return Ok(None),
            Some("calendar") => Screen::Month {
                grid: part(&v["grid"])?,
                side: part(&v["side"])?,
                hints: part(&v["hints"])?,
            },
            Some(name @ ("day" | "job" | "form")) => {
                let mut count = 0;
                Screen::Form {
                    // The job form and /modelcfg scroll in Python as focus moves.
                    job: name != "day",
                    content: region(&v["content"])?,
                    scroll_y: v["scroll_y"].as_u64().ok_or_else(bad)? as u16,
                    max_scroll_y: v["max_scroll_y"].as_u64().ok_or_else(bad)? as u16,
                    focused: v["focused"].as_str().map(str::to_owned),
                    bar: match v.get("bar") {
                        None | Some(Value::Null) => None,
                        Some(b) => Some(region(b)?),
                    },
                    items: v["items"]
                        .as_array()
                        .ok_or_else(bad)?
                        .iter()
                        .map(|i| item(i, 0, &mut count))
                        .collect::<io::Result<Vec<_>>>()?,
                }
            }
            _ => return Err(bad()),
        };
        let size = v["size"]
            .as_array()
            .filter(|s| s.len() == 2)
            .ok_or_else(bad)?;
        let dock = match &v["dock"] {
            Value::Null => None,
            d => Some((region(&d["region"])?, text(d, "label")?)),
        };
        Ok(Some(Self {
            frame: Rect::new(
                0,
                0,
                size[0].as_u64().ok_or_else(bad)? as u16,
                size[1].as_u64().ok_or_else(bad)? as u16,
            ),
            boxed: region(&v["box"])?,
            dock,
            screen,
        }))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Open {
        size: [u16; 2],
    },
    Act {
        size: [u16; 2],
        values: Map<String, Value>,
        action: Value,
    },
    Read {
        size: [u16; 2],
    },
    Close,
}
impl Intent {
    pub fn command(&self) -> Value {
        self.command_for(Kind::Calendar)
    }
    pub fn command_for(&self, kind: Kind) -> Value {
        let [open, act, read, close] = kind.operations();
        match self {
            Intent::Open { size } => json!({"type": open, "version": 1, "size": size}),
            Intent::Read { size } => json!({"type": read, "version": 1, "size": size}),
            Intent::Close => json!({"type": close, "version": 1}),
            Intent::Act {
                size,
                values,
                action,
            } => json!({
                "type": act, "version": 1, "size": size, "values": values, "action": action
            }),
        }
    }
    pub fn closing(&self) -> bool {
        matches!(self, Intent::Close)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    /// The top screen, or None once the chain has closed itself.
    Snapshot(Option<Box<Snapshot>>),
    /// /modelcfg on a remote backend: Python's answer is an effort picker, not this screen.
    Remote,
    Closed,
    Rejected(String),
}

#[derive(Clone, Debug, PartialEq)]
enum Hit {
    Grid(Rect),
    Side(Rect),
    Control(String),
    Option(String, usize),
    Tick(String, i64),
    Drop(usize),
    Tab(String),
}

#[derive(Default)]
pub struct View {
    pub kind: Kind,
    /// Set when /modelcfg met a remote backend: the frontend opens its effort route.
    pub remote: bool,
    pub open: bool,
    waiting: bool,
    snapshot: Option<Snapshot>,
    /// Unsent edits: Input text, Switch values, a ticker's typed number.
    edits: BTreeMap<String, Value>,
    focus: usize,
    cursor: usize,
    /// The day list's highlighted row, moved here and picked with Enter.
    highlight: Option<usize>,
    dropdown: Option<(String, usize)>,
    hits: Vec<(Rect, Hit)>,
    intent: Option<Intent>,
    size: [u16; 2],
    pub notice: String,
}

struct Paint {
    surface: Color,
    text: Color,
    muted: Color,
    border: Color,
    accent: Color,
    panel: Color,
    error: Color,
    background: Color,
}
impl Paint {
    fn of(t: &Theme) -> Self {
        Self {
            surface: t.surface,
            text: t.foreground,
            muted: t.thinking_text,
            border: t.thinking_border,
            accent: t.primary,
            panel: t.panel,
            error: t.error,
            background: t.background,
        }
    }
}

/// Rich's style string on top of `base`: bold, dim, italic, underline, strike, reverse, a
/// `#rrggbb` colour and `on #rrggbb`.
pub fn rich_style(spec: &str, base: Style) -> Style {
    let mut style = base;
    let mut words = spec.split_whitespace();
    let hex = |w: &str| -> Option<Color> {
        let h = w.strip_prefix('#').filter(|h| h.len() == 6)?;
        let n = u32::from_str_radix(h, 16).ok()?;
        Some(Color::Rgb((n >> 16) as u8, (n >> 8) as u8, n as u8))
    };
    while let Some(word) = words.next() {
        match word {
            "bold" => style = style.add_modifier(Modifier::BOLD),
            "dim" => style = style.add_modifier(Modifier::DIM),
            "italic" => style = style.add_modifier(Modifier::ITALIC),
            "underline" => style = style.add_modifier(Modifier::UNDERLINED),
            "strike" => style = style.add_modifier(Modifier::CROSSED_OUT),
            "reverse" => style = style.add_modifier(Modifier::REVERSED),
            "on" => {
                if let Some(c) = words.next().and_then(hex) {
                    style = style.bg(c);
                }
            }
            w => {
                if let Some(c) = hex(w) {
                    style = style.fg(c);
                }
            }
        }
    }
    style
}

fn clip(text: &str, cells: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|c| {
            used += c.width().unwrap_or(0);
            used <= cells
        })
        .collect()
}
fn pad(text: &str, cells: usize) -> String {
    let t = clip(text, cells);
    let w = t.width();
    format!("{t}{}", " ".repeat(cells.saturating_sub(w)))
}

impl View {
    pub fn owns_input(&self) -> bool {
        self.open
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    fn size(&self) -> [u16; 2] {
        if self.size[0] >= 10 && self.size[1] >= 10 {
            self.size
        } else {
            crossterm::terminal::size()
                .map(|(w, h)| [w.max(10), h.max(10)])
                .unwrap_or([80, 24])
        }
    }
    /// The frame size this view is drawn in (the terminal), sent with every command.
    pub fn set_size(&mut self, area: Rect) {
        self.size = [area.width, area.height];
    }
    pub fn open(&mut self) {
        self.open_kind(Kind::Calendar);
    }
    pub fn open_kind(&mut self, kind: Kind) {
        if self.waiting {
            return;
        }
        let size = self.size();
        *self = Self {
            kind,
            open: true,
            waiting: true,
            size,
            intent: Some(Intent::Open { size }),
            ..Self::default()
        };
    }
    fn act(&mut self, action: Value) {
        if self.waiting {
            return;
        }
        self.dropdown = None;
        self.waiting = true;
        self.intent = Some(Intent::Act {
            size: self.size(),
            values: self.edits.clone().into_iter().collect(),
            action,
        });
    }
    pub fn observe(&mut self, update: Update) {
        self.waiting = false;
        match update {
            Update::Snapshot(Some(snapshot)) => {
                let screen_changed = !matches!(
                    (&self.snapshot, &snapshot.screen),
                    (Some(Snapshot { screen: Screen::Form { job: a, .. }, .. }), Screen::Form { job: b, .. }) if a == b
                ) && !matches!(
                    (&self.snapshot, &snapshot.screen),
                    (
                        Some(Snapshot {
                            screen: Screen::Month { .. },
                            ..
                        }),
                        Screen::Month { .. }
                    )
                );
                self.snapshot = Some(*snapshot);
                self.edits.clear();
                self.notice.clear();
                self.dropdown = None;
                let focused = match &self.snapshot.as_ref().expect("snapshot").screen {
                    Screen::Form { focused, .. } => focused.clone(),
                    Screen::Month { .. } => None,
                };
                let stops = self.stops();
                if let Some(index) = focused.and_then(|f| stops.iter().position(|s| *s == f)) {
                    self.focus = index;
                } else if screen_changed {
                    self.focus = 0;
                }
                self.focus = self.focus.min(stops.len().saturating_sub(1));
                if screen_changed {
                    self.highlight = self.options().and_then(|(_, _, h)| h);
                }
                self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count());
            }
            Update::Remote => {
                *self = Self {
                    kind: self.kind,
                    size: self.size,
                    remote: true,
                    ..Self::default()
                };
            }
            Update::Snapshot(None) | Update::Closed => {
                if self.open && matches!(update, Update::Snapshot(None)) {
                    // The month closed itself in Python; the session is over there too.
                    *self = Self {
                        size: self.size,
                        ..Self::default()
                    };
                    return;
                }
                *self = Self {
                    size: self.size,
                    ..Self::default()
                };
            }
            Update::Rejected(reason) => {
                self.notice = reason;
                if self.snapshot.is_none() {
                    *self = Self {
                        size: self.size,
                        notice: std::mem::take(&mut self.notice),
                        ..Self::default()
                    };
                }
            }
        }
    }

    fn items(&self) -> Vec<&Item> {
        fn visit<'a>(items: &'a [Item], out: &mut Vec<&'a Item>) {
            for i in items {
                match &i.control {
                    Control::Line(children)
                    | Control::Clip {
                        items: children, ..
                    } => visit(children, out),
                    _ => out.push(i),
                }
            }
        }
        let mut out = Vec::new();
        if let Some(Snapshot {
            screen: Screen::Form { items, .. },
            ..
        }) = &self.snapshot
        {
            visit(items, &mut out);
        }
        out
    }
    /// Textual's focus chain on the day or job screen: its focusable widgets in order.
    fn stops(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        if self.tab_strip().is_some() {
            out.push(TABS.to_owned());
        }
        out.extend(
            self.items()
                .into_iter()
                .filter(|i| {
                    !i.disabled && !matches!(i.control, Control::Text { .. } | Control::Tabs { .. })
                })
                .filter_map(|i| i.id.clone()),
        );
        out
    }
    fn tab_strip(&self) -> Option<TabStrip> {
        self.items().into_iter().find_map(|i| match &i.control {
            Control::Tabs { tabs, active } if !tabs.is_empty() => {
                Some((tabs.clone(), active.clone()))
            }
            _ => None,
        })
    }
    fn focused(&self) -> Option<String> {
        self.stops().get(self.focus).cloned()
    }
    fn item(&self, id: &str) -> Option<&Item> {
        self.items()
            .into_iter()
            .find(|i| i.id.as_deref() == Some(id))
    }
    fn options(&self) -> Option<(String, Vec<Opt>, Option<usize>)> {
        self.items().into_iter().find_map(|i| match &i.control {
            Control::Options {
                options,
                highlighted,
            } => Some((i.id.clone()?, options.clone(), *highlighted)),
            _ => None,
        })
    }
    fn text_value(&self, item: &Item) -> Option<String> {
        let id = item.id.as_deref()?;
        match (self.edits.get(id), &item.control) {
            (Some(Value::String(s)), _) => Some(s.clone()),
            (Some(Value::Number(n)), _) => Some(format!("{:02}", n.as_i64().unwrap_or(0))),
            (_, Control::Input { value, .. }) => Some(value.clone()),
            (_, Control::Ticker { text, .. }) => Some(text.clone()),
            _ => None,
        }
    }
    fn switch_value(&self, item: &Item) -> bool {
        match (
            item.id.as_deref().and_then(|id| self.edits.get(id)),
            &item.control,
        ) {
            (Some(Value::Bool(b)), _) => *b,
            (_, Control::Switch { value }) => *value,
            _ => false,
        }
    }
    fn text_of_focus(&self) -> Option<String> {
        let id = self.focused()?;
        self.item(&id).and_then(|i| self.text_value(i))
    }
    fn is_job(&self) -> bool {
        matches!(
            self.snapshot,
            Some(Snapshot {
                screen: Screen::Form { job: true, .. },
                ..
            })
        )
    }
    fn is_month(&self) -> bool {
        matches!(
            self.snapshot,
            Some(Snapshot {
                screen: Screen::Month { .. },
                ..
            })
        )
    }
    /// Move the focus; on the job form Python takes it too, so Textual scrolls it into view.
    fn set_focus(&mut self, index: usize) {
        let stops = self.stops();
        if stops.is_empty() {
            return;
        }
        self.focus = index % stops.len();
        self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count());
        if self.is_job() && stops[self.focus] != TABS {
            let id = stops[self.focus].clone();
            self.act(json!({"focus": id}));
        }
    }
    fn step_focus(&mut self, delta: isize) {
        let n = self.stops().len() as isize;
        if n > 0 {
            self.set_focus((self.focus as isize + delta).rem_euclid(n) as usize);
        }
    }
    fn press(&mut self, id: &str) {
        let Some(item) = self.item(id).cloned() else {
            return;
        };
        if item.disabled {
            return;
        }
        match item.control {
            // Dock to side stays inert, as on every native dialog so far.
            Control::Button { .. } if id == "dialog-swap" => {}
            Control::Button { .. } => self.act(json!({"press": id})),
            Control::Switch { .. } => {
                let on = !self.switch_value(&item);
                self.edits.insert(id.to_owned(), Value::Bool(on));
            }
            Control::Select { value, options, .. } if !options.is_empty() => {
                self.dropdown = Some((id.to_owned(), value.unwrap_or(0)));
            }
            Control::Options { .. } => {
                if let Some(index) = self.highlight {
                    self.act(json!({"option": id, "index": index}));
                }
            }
            _ => {}
        }
    }
    fn pick(&mut self, id: &str, index: usize) {
        let current = match self.item(id).map(|i| &i.control) {
            Some(Control::Select { value, .. }) => *value,
            _ => None,
        };
        self.dropdown = None;
        if current != Some(index) {
            self.act(json!({"select": id, "index": index}));
        }
    }
    fn edit(&mut self, change: impl FnOnce(&mut String, &mut usize)) {
        let Some(id) = self.focused() else {
            return;
        };
        let Some(item) = self.item(&id).cloned() else {
            return;
        };
        let ticker = matches!(item.control, Control::Ticker { .. });
        if !matches!(item.control, Control::Input { .. }) && !ticker {
            return;
        }
        let mut text = self.text_value(&item).unwrap_or_default();
        let mut cursor = self.cursor.min(text.chars().count());
        change(&mut text, &mut cursor);
        self.cursor = cursor;
        if ticker {
            // A ticker takes digits only; its handler clamps on the next act.
            let digits: String = text.chars().filter(char::is_ascii_digit).take(4).collect();
            self.cursor = self.cursor.min(digits.chars().count());
            if let Ok(n) = digits.parse::<i64>() {
                self.edits.insert(id, Value::from(n));
            } else {
                self.edits.insert(id, Value::String(digits));
            }
        } else {
            self.edits.insert(id, Value::String(text));
        }
    }
    fn byte(text: &str, chars: usize) -> usize {
        text.char_indices()
            .nth(chars)
            .map_or(text.len(), |(i, _)| i)
    }
    pub fn paste(&mut self, pasted: &str) {
        if self.waiting || self.dropdown.is_some() {
            return;
        }
        let line = pasted.lines().next().unwrap_or("").to_owned();
        self.edit(|text, cursor| {
            let at = Self::byte(text, *cursor);
            text.insert_str(at, &line);
            *cursor += line.chars().count();
        });
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !self.open || self.waiting {
            return;
        }
        if let Some((id, highlighted)) = self.dropdown.clone() {
            let count = match self.item(&id).map(|i| &i.control) {
                Some(Control::Select { options, .. }) => options.len(),
                _ => 0,
            };
            match key.code {
                KeyCode::Esc => self.dropdown = None,
                KeyCode::Up => self.dropdown = Some((id, highlighted.saturating_sub(1))),
                KeyCode::Down => {
                    self.dropdown = Some((id, (highlighted + 1).min(count.saturating_sub(1))))
                }
                KeyCode::Enter | KeyCode::Char(' ') => self.pick(&id, highlighted),
                _ => {}
            }
            return;
        }
        if self.is_month() {
            let name = match key.code {
                KeyCode::Left => "left",
                KeyCode::Right => "right",
                KeyCode::Esc => "escape",
                KeyCode::Char(c @ ('h' | 'l' | 't' | 'q')) if key.modifiers.is_empty() => match c {
                    'h' => "h",
                    'l' => "l",
                    't' => "t",
                    _ => "q",
                },
                _ => return,
            };
            self.act(json!({"key": name}));
            return;
        }
        let focused = self.focused();
        let item = focused.as_deref().and_then(|id| self.item(id)).cloned();
        let typing = matches!(
            item.as_ref().map(|i| &i.control),
            Some(Control::Input { .. } | Control::Ticker { .. })
        );
        match key.code {
            KeyCode::Esc => self.act(json!({"key": "escape"})),
            // /modelcfg: Ctrl+S is Python's apply (a loaded model reloads).
            KeyCode::Char('s')
                if self.kind == Kind::ModelConfig && key.modifiers == KeyModifiers::CONTROL =>
            {
                self.act(json!({"key": "ctrl+s"}))
            }
            KeyCode::Left | KeyCode::Right if self.focused().as_deref() == Some(TABS) => {
                if let Some((tabs, active)) = self.tab_strip() {
                    let at = tabs
                        .iter()
                        .position(|(p, _, _)| Some(p) == active.as_ref())
                        .unwrap_or(0);
                    let next = if key.code == KeyCode::Left {
                        (at + tabs.len() - 1) % tabs.len()
                    } else {
                        (at + 1) % tabs.len()
                    };
                    self.act(json!({"tab": tabs[next].0}));
                }
            }
            KeyCode::Tab if key.modifiers.is_empty() => self.step_focus(1),
            KeyCode::BackTab => self.step_focus(-1),
            // The day popup's keys (n new, q close), when the list or Dock has focus.
            KeyCode::Char(c @ ('n' | 'q')) if !self.is_job() && key.modifiers.is_empty() => {
                self.act(json!({"key": c.to_string()}))
            }
            KeyCode::Up | KeyCode::Down
                if matches!(
                    item.as_ref().map(|i| &i.control),
                    Some(Control::Options { .. })
                ) =>
            {
                if let Some((_, options, _)) = self.options() {
                    let enabled: Vec<usize> = (0..options.len())
                        .filter(|i| !options[*i].disabled)
                        .collect();
                    let at = self
                        .highlight
                        .and_then(|h| enabled.iter().position(|i| *i == h));
                    let next = match (key.code, at) {
                        (KeyCode::Up, Some(p)) => p.saturating_sub(1),
                        (KeyCode::Down, Some(p)) => (p + 1).min(enabled.len().saturating_sub(1)),
                        _ => 0,
                    };
                    self.highlight = enabled.get(next).copied();
                }
            }
            KeyCode::Up | KeyCode::Down
                if matches!(
                    item.as_ref().map(|i| &i.control),
                    Some(Control::Ticker { .. })
                ) =>
            {
                let id = focused.clone().expect("ticker");
                let delta = if key.code == KeyCode::Up { 1 } else { -1 };
                self.act(json!({"tick": id, "delta": delta}));
            }
            KeyCode::Up | KeyCode::Down
                if matches!(
                    item.as_ref().map(|i| &i.control),
                    Some(Control::Select { .. })
                ) =>
            {
                if let Some(id) = focused {
                    self.press(&id);
                }
            }
            KeyCode::Enter if typing => {
                // Enter in any field is Python's save attempt (JobBody._submitted).
                if let Some(id) = focused {
                    self.act(json!({"submit": id}));
                }
            }
            KeyCode::Enter | KeyCode::Char(' ') if !typing => {
                if let Some(id) = focused {
                    self.press(&id);
                }
            }
            KeyCode::Char(c)
                if typing && (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT) =>
            {
                self.edit(|text, cursor| {
                    let at = Self::byte(text, *cursor);
                    text.insert(at, c);
                    *cursor += 1;
                })
            }
            KeyCode::Backspace if typing => self.edit(|text, cursor| {
                if *cursor > 0 {
                    let at = Self::byte(text, *cursor - 1);
                    text.remove(at);
                    *cursor -= 1;
                }
            }),
            KeyCode::Delete if typing => self.edit(|text, cursor| {
                if *cursor < text.chars().count() {
                    let at = Self::byte(text, *cursor);
                    text.remove(at);
                }
            }),
            KeyCode::Left if typing => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right if typing => {
                let n = self.text_of_focus().map_or(0, |t| t.chars().count());
                self.cursor = (self.cursor + 1).min(n);
            }
            KeyCode::Home if typing => self.cursor = 0,
            KeyCode::End if typing => {
                self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count())
            }
            KeyCode::PageUp if self.is_job() => self.act(json!({"scroll": -10})),
            KeyCode::PageDown if self.is_job() => self.act(json!({"scroll": 10})),
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if !self.open || self.waiting {
            return;
        }
        let at = (event.column, event.row).into();
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                let hit = self.hits.iter().find(|(r, _)| r.contains(at)).cloned();
                if let Some((id, _)) = self.dropdown.clone() {
                    match hit {
                        Some((_, Hit::Drop(index))) => self.pick(&id, index),
                        _ => self.dropdown = None,
                    }
                    return;
                }
                let Some((rect, hit)) = hit else {
                    return;
                };
                match hit {
                    Hit::Grid(r) => self.act(json!({"grid_click": [
                        event.column.saturating_sub(r.x), event.row.saturating_sub(r.y)
                    ]})),
                    Hit::Side(r) => self.act(json!({"side_click": [
                        event.column.saturating_sub(r.x), event.row.saturating_sub(r.y)
                    ]})),
                    Hit::Option(id, index) => {
                        self.highlight = Some(index);
                        self.act(json!({"option": id, "index": index}));
                    }
                    Hit::Tick(id, delta) => self.act(json!({"tick": id, "delta": delta})),
                    Hit::Control(id) => {
                        let _ = rect;
                        let index = self.stops().iter().position(|s| *s == id);
                        let typing = matches!(
                            self.item(&id).map(|i| &i.control),
                            Some(Control::Input { .. } | Control::Ticker { .. })
                        );
                        if let Some(index) = index
                            && index != self.focus
                        {
                            self.focus = index;
                            self.cursor = self.text_of_focus().map_or(0, |t| t.chars().count());
                            if typing && self.is_job() {
                                // Python takes the focus (and scrolls it into view).
                                self.act(json!({"focus": id}));
                                return;
                            }
                        }
                        if !typing {
                            self.press(&id);
                        }
                    }
                    Hit::Drop(_) => {}
                    Hit::Tab(pane) => self.act(json!({"tab": pane})),
                }
            }
            // One notch: Python's own wheel handling (App.scroll_sensitivity_y lines).
            MouseEventKind::ScrollDown if self.is_job() => self.act(json!({"wheel": 1})),
            MouseEventKind::ScrollUp if self.is_job() => self.act(json!({"wheel": -1})),
            _ => {}
        }
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        self.set_size(area);
        self.hits.clear();
        if !self.open {
            return;
        }
        let p = Paint::of(theme);
        frame.render_widget(Clear, area);
        let buf = frame.buffer_mut();
        let back = Style::default().bg(p.background);
        for y in area.top()..area.bottom() {
            for x in area.left()..area.right() {
                buf[(x, y)].set_symbol(" ").set_style(back);
            }
        }
        let Some(snapshot) = self.snapshot.clone() else {
            let msg = if self.notice.is_empty() {
                "Opening the calendar…"
            } else {
                self.notice.as_str()
            };
            buf.set_stringn(
                area.x + 2,
                area.y + 1,
                msg,
                usize::from(area.width.saturating_sub(4)),
                Style::default().fg(p.muted).bg(p.background),
            );
            return;
        };
        let clip_area = area;
        let boxed = snapshot.boxed.intersection(clip_area);
        thick_box(buf, boxed, &p);
        let surface = Style::default().fg(p.text).bg(p.surface);
        match &snapshot.screen {
            Screen::Month { grid, side, hints } => {
                draw_lines(buf, grid.region, &grid.lines, 0, surface);
                self.hits.push((grid.region, Hit::Grid(grid.region)));
                // border-left: solid $primary, then padding 0 0 0 2.
                for y in side.region.top()..side.region.bottom().min(area.bottom()) {
                    buf[(side.region.x, y)]
                        .set_symbol("│")
                        .set_style(Style::default().fg(p.accent).bg(p.surface));
                }
                let text_area = Rect::new(
                    side.region.x + 3,
                    side.region.y,
                    side.region.width.saturating_sub(3),
                    side.region.height,
                );
                draw_lines(buf, text_area, &side.lines, 0, surface);
                self.hits.push((text_area, Hit::Side(text_area)));
                let hints_area = Rect::new(
                    hints.region.x + 1,
                    hints.region.y,
                    hints.region.width.saturating_sub(2),
                    hints.region.height,
                );
                draw_lines(buf, hints_area, &hints.lines, 0, surface);
            }
            Screen::Form {
                content,
                scroll_y,
                max_scroll_y,
                items,
                bar,
                ..
            } => {
                let focused = self.focused();
                // Textual clips a scrolled box's children to its content area (the month
                // Select runs past it): draw them aside, then copy only that area.
                let mut scratch = buf.clone();
                for item in items {
                    self.draw_item(&mut scratch, item, *content, focused.as_deref(), &p);
                }
                let shown = content.intersection(area);
                for y in shown.top()..shown.bottom() {
                    for x in shown.left()..shown.right() {
                        buf[(x, y)] = scratch[(x, y)].clone();
                    }
                }
                self.hits.retain(|(r, _)| r.x < content.right());
                for (r, _) in self.hits.iter_mut() {
                    *r = r.intersection(*content);
                }
                if *max_scroll_y > 0 {
                    let at =
                        bar.unwrap_or(Rect::new(content.right(), content.y, 2, content.height));
                    scrollbar(buf, at, *scroll_y, *max_scroll_y, &p);
                }
            }
        }
        if let Some((r, label)) = &snapshot.dock
            && !matches!(snapshot.screen, Screen::Form { .. })
        {
            button(buf, *r, label, "default", false, false, &p, boxed);
        }
        if !self.notice.is_empty() {
            buf.set_stringn(
                boxed.x + 3,
                boxed.bottom().saturating_sub(1),
                format!(" {} ", self.notice),
                usize::from(boxed.width.saturating_sub(6)),
                Style::default().fg(p.error).bg(p.surface),
            );
        }
        // An open Select's options, over everything.
        if let Some((id, highlighted)) = self.dropdown.clone()
            && let Some(item) = self.item(&id).cloned()
            && let Control::Select { options, .. } = &item.control
        {
            let x = item.region.x;
            let w = item.region.width;
            let height = (options.len() as u16 + 2).min(area.height.saturating_sub(2));
            let below = item.region.bottom();
            let y = if below + height <= area.bottom() {
                below.saturating_sub(1)
            } else {
                area.bottom().saturating_sub(height)
            };
            let drop = Rect::new(x, y, w, height);
            let edge = Style::default().fg(p.accent).bg(p.surface);
            tall_rows(buf, drop, edge);
            let shown = usize::from(height.saturating_sub(2));
            let first = highlighted.saturating_sub(shown.saturating_sub(1));
            for (row, (index, option)) in options
                .iter()
                .enumerate()
                .skip(first)
                .take(shown)
                .enumerate()
            {
                let style = if index == highlighted {
                    Style::default()
                        .fg(p.background)
                        .bg(p.accent)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(p.text).bg(p.surface)
                };
                let r = Rect::new(x + 1, y + 1 + row as u16, w.saturating_sub(2), 1);
                buf.set_stringn(
                    r.x,
                    r.y,
                    format!(" {}", pad(option, usize::from(r.width).saturating_sub(1))),
                    usize::from(r.width),
                    style,
                );
                self.hits.insert(0, (r, Hit::Drop(index)));
            }
        }
    }

    fn draw_item(
        &mut self,
        buf: &mut Buffer,
        item: &Item,
        content: Rect,
        focused: Option<&str>,
        p: &Paint,
    ) {
        let r = item.region;
        let is_focus = item.id.is_some() && item.id.as_deref() == focused;
        let visible = r.intersection(content);
        if visible.is_empty() && !matches!(item.control, Control::Line(_) | Control::Clip { .. }) {
            return;
        }
        if let Control::Clip {
            items,
            clip,
            scroll_y,
            max_scroll_y,
            bar,
        } = &item.control
        {
            // Cut the children to the container's own content area, as Textual does.
            let cut = clip.intersection(content);
            let mut inner = buf.clone();
            for child in items {
                self.draw_item(&mut inner, child, cut, focused, p);
            }
            for y in cut.top()..cut.bottom() {
                for x in cut.left()..cut.right() {
                    buf[(x, y)] = inner[(x, y)].clone();
                }
            }
            if *max_scroll_y > 0 {
                let at = bar.unwrap_or(Rect::new(clip.right(), clip.y, 2, clip.height));
                scrollbar(buf, at, *scroll_y, *max_scroll_y, p);
            }
            return;
        }
        if let Control::Tabs { tabs, active } = &item.control {
            let label = Style::default().fg(p.muted).bg(p.surface);
            let lit = Style::default()
                .fg(p.text)
                .bg(p.surface)
                .add_modifier(Modifier::BOLD);
            let bar = Style::default().fg(p.border).bg(p.surface);
            let hot = Style::default().fg(p.accent).bg(p.surface);
            let line = r.y + 1;
            for x in r.left()..r.right() {
                if line < buf.area.bottom() {
                    buf[(x, line)].set_symbol("━").set_style(bar);
                }
            }
            for (pane, text, t) in tabs {
                let on = active.as_ref() == Some(pane);
                buf.set_stringn(
                    t.x,
                    t.y,
                    format!(" {text} "),
                    usize::from(t.width),
                    if on { lit } else { label },
                );
                if on && line < buf.area.bottom() {
                    // Textual's Underline: the active label in the accent, the bars capped.
                    let (start, end) = (t.x + 1, t.x + 1 + text.width() as u16);
                    for x in start..end.min(r.right()) {
                        buf[(x, line)].set_symbol("━").set_style(hot);
                    }
                    if start > r.x {
                        buf[(start - 1, line)].set_symbol("╸").set_style(bar);
                    }
                    if end < r.right() {
                        buf[(end, line)].set_symbol("╺").set_style(bar);
                    }
                }
                self.hits
                    .push((t.intersection(content), Hit::Tab(pane.clone())));
            }
            return;
        }
        let surface = Style::default().fg(p.text).bg(p.surface);
        let muted = Style::default().fg(p.muted).bg(p.surface);
        let edge = Style::default()
            .fg(if is_focus { p.accent } else { p.border })
            .bg(p.surface);
        match &item.control {
            Control::Line(children) => {
                for child in children {
                    self.draw_item(buf, child, content, focused, p);
                }
            }
            Control::Clip { .. } | Control::Tabs { .. } => {}
            Control::Text { text, classes } => {
                let id = item.id.as_deref().unwrap_or("");
                // .set-label, .set-label-inline and .set-help: padding-left 1.
                let indent = u16::from(
                    classes
                        .iter()
                        .any(|c| c == "set-label" || c == "set-label-inline" || c == "set-help"),
                );
                let style = if id == "set-title" {
                    Style::default()
                        .fg(p.accent)
                        .bg(p.surface)
                        .add_modifier(Modifier::BOLD)
                } else if classes
                    .iter()
                    .any(|c| c == "set-label" || c == "set-label-inline")
                {
                    Style::default()
                        .fg(p.text)
                        .bg(p.surface)
                        .add_modifier(Modifier::BOLD)
                } else if id == "set-sub" || classes.iter().any(|c| c == "set-help") {
                    muted
                } else if id == "mc-error" {
                    Style::default().fg(p.error).bg(p.surface)
                } else if id.ends_with("-title") {
                    Style::default()
                        .fg(p.error)
                        .bg(p.surface)
                        .add_modifier(Modifier::BOLD)
                } else if classes.iter().any(|c| {
                    c.starts_with("job-cap") || c.ends_with("cap") || c == "job-param-label"
                }) || id == "day-hint"
                    || id.starts_with("job-lbl")
                    || id == "job-unit"
                {
                    muted
                } else if id == "job-status" && text.starts_with('✗') {
                    Style::default()
                        .fg(p.error)
                        .bg(p.surface)
                        .add_modifier(Modifier::BOLD)
                } else if id == "job-status" {
                    muted
                } else {
                    surface
                };
                let width = r.width.saturating_sub(indent);
                for (i, line) in crate::picker_box::wrap(text, usize::from(width))
                    .iter()
                    .enumerate()
                    .take(usize::from(r.height))
                {
                    let y = r.y + i as u16;
                    if y >= content.top() && y < content.bottom() {
                        buf.set_stringn(r.x + indent, y, line, usize::from(width), style);
                    }
                }
            }
            Control::Input { placeholder, .. } => {
                let value = self.text_value(item).unwrap_or_default();
                let (shown, style) = if value.is_empty() {
                    (placeholder.clone(), muted)
                } else {
                    (value, surface)
                };
                let body = usize::from(r.width.saturating_sub(6));
                let start = if is_focus {
                    self.cursor.saturating_sub(body.saturating_sub(1))
                } else {
                    0
                };
                let visible_text: String = shown.chars().skip(start).collect();
                tall(
                    buf,
                    r,
                    edge,
                    &format!("  {}  ", pad(&visible_text, body)),
                    style,
                    content,
                );
                self.hits
                    .push((visible, Hit::Control(item.id.clone().unwrap_or_default())));
            }
            Control::Select {
                prompt,
                options,
                value,
            } => {
                let shown = value.and_then(|i| options.get(i)).cloned();
                let style = if shown.is_some() { surface } else { muted };
                let inner = usize::from(r.width.saturating_sub(2));
                let body = inner.saturating_sub(5);
                let text = shown.as_deref().unwrap_or(prompt);
                let wrapped = crate::picker_box::wrap(text, body);
                let rows = usize::from(r.height.saturating_sub(2)).max(1);
                let mut lines: Vec<String> = Vec::new();
                for i in 0..rows {
                    let piece = wrapped.get(i).cloned().unwrap_or_default();
                    if i == 0 {
                        lines.push(format!("  {}▼  ", pad(&piece, body)));
                    } else {
                        lines.push(format!("  {}   ", pad(&piece, body)));
                    }
                }
                tall_lines(buf, r, edge, &lines, style, content);
                self.hits
                    .push((visible, Hit::Control(item.id.clone().unwrap_or_default())));
            }
            Control::Switch { .. } => {
                let on = self.switch_value(item);
                tall(
                    buf,
                    r,
                    edge,
                    &" ".repeat(usize::from(r.width.saturating_sub(2))),
                    surface,
                    content,
                );
                let y = r.y + 1;
                if y >= content.top() && y < content.bottom() {
                    let knob = if on { r.x + 5 } else { r.x + 3 };
                    buf.set_stringn(
                        knob,
                        y,
                        "  ",
                        2,
                        Style::default().bg(if on { p.accent } else { p.border }),
                    );
                }
                self.hits
                    .push((visible, Hit::Control(item.id.clone().unwrap_or_default())));
            }
            Control::Button { label, variant } => {
                button(buf, r, label, variant, is_focus, item.disabled, p, content);
                self.hits
                    .push((visible, Hit::Control(item.id.clone().unwrap_or_default())));
            }
            Control::Options { options, .. } => {
                tall_rows_clipped(buf, r, edge, content);
                let id = item.id.clone().unwrap_or_default();
                let shown = usize::from(r.height.saturating_sub(2));
                for (row, (index, option)) in options.iter().enumerate().take(shown).enumerate() {
                    let y = r.y + 1 + row as u16;
                    if y < content.top() || y >= content.bottom() {
                        continue;
                    }
                    let lit = self.highlight == Some(index);
                    let base = if option.disabled {
                        muted
                    } else if lit && is_focus {
                        Style::default()
                            .fg(p.background)
                            .bg(p.accent)
                            .add_modifier(Modifier::BOLD)
                    } else if lit {
                        Style::default().fg(p.text).bg(p.panel)
                    } else {
                        surface
                    };
                    let row_area = Rect::new(r.x + 1, y, r.width.saturating_sub(2), 1);
                    for x in row_area.left()..row_area.right() {
                        buf[(x, y)].set_symbol(" ").set_style(base);
                    }
                    let first: Vec<Vec<Run>> = option.lines.iter().take(1).cloned().collect();
                    draw_lines(
                        buf,
                        Rect::new(row_area.x + 1, y, row_area.width.saturating_sub(2), 1),
                        &first,
                        0,
                        base,
                    );
                    if !option.disabled {
                        self.hits.push((row_area, Hit::Option(id.clone(), index)));
                    }
                }
            }
            Control::Ticker { .. } => {
                // NumberTicker: "-" (5), its Input (8), "+" (5), each 3 rows.
                let id = item.id.clone().unwrap_or_default();
                let minus = Rect::new(r.x, r.y, 5, r.height);
                let input = Rect::new(r.x + 5, r.y, 8, r.height);
                let plus = Rect::new(r.x + 13, r.y, 5, r.height);
                button(buf, minus, "−", "default", false, item.disabled, p, content);
                button(buf, plus, "+", "default", false, item.disabled, p, content);
                let value = self.text_value(item).unwrap_or_default();
                tall(
                    buf,
                    input,
                    edge,
                    &format!("  {}  ", pad(&value, 2)),
                    surface,
                    content,
                );
                self.hits
                    .push((minus.intersection(content), Hit::Tick(id.clone(), -1)));
                self.hits
                    .push((plus.intersection(content), Hit::Tick(id.clone(), 1)));
                self.hits
                    .push((input.intersection(content), Hit::Control(id)));
            }
        }
    }
}

fn thick_box(buf: &mut Buffer, r: Rect, p: &Paint) {
    if r.width < 2 || r.height < 2 {
        return;
    }
    let edge = Style::default().fg(p.accent).bg(p.surface);
    let fill = Style::default().fg(p.text).bg(p.surface);
    for y in r.top()..r.bottom() {
        for x in r.left()..r.right() {
            let symbol = if x == r.left() || x == r.right() - 1 {
                "█"
            } else if y == r.top() {
                "▀"
            } else if y == r.bottom() - 1 {
                "▄"
            } else {
                " "
            };
            let style = if symbol == " " { fill } else { edge };
            buf[(x, y)].set_symbol(symbol).set_style(style);
        }
    }
}

/// Styled lines drawn into `area` (cropped, as Rich's no_wrap Text is).
fn draw_lines(buf: &mut Buffer, area: Rect, lines: &Lines, skip: usize, base: Style) {
    for (row, line) in lines
        .iter()
        .skip(skip)
        .take(usize::from(area.height))
        .enumerate()
    {
        let y = area.y + row as u16;
        let mut x = area.x;
        let end = area.right();
        for (text, style) in line {
            for ch in text.chars() {
                let w = ch.width().unwrap_or(0) as u16;
                if w == 0 {
                    continue;
                }
                if x + w > end {
                    break;
                }
                buf[(x, y)]
                    .set_symbol(&ch.to_string())
                    .set_style(rich_style(style, base));
                x += w;
            }
        }
    }
}

fn tall_lines(buf: &mut Buffer, r: Rect, edge: Style, body: &[String], style: Style, clip: Rect) {
    let inner = usize::from(r.width.saturating_sub(2));
    let rows = r.height;
    for i in 0..rows {
        let y = r.y + i;
        if y < clip.top() || y >= clip.bottom() {
            continue;
        }
        let (fill, fill_style) = if i == 0 {
            ("▔".repeat(inner), edge)
        } else if i == rows - 1 {
            ("▁".repeat(inner), edge)
        } else {
            (
                pad(
                    body.get(usize::from(i - 1)).map_or("", String::as_str),
                    inner,
                ),
                style,
            )
        };
        buf.set_stringn(r.x, y, "▊", 1, edge);
        buf.set_stringn(r.x + 1, y, &fill, inner, fill_style);
        buf.set_stringn(r.x + 1 + inner as u16, y, "▎", 1, edge);
    }
}
fn tall(buf: &mut Buffer, r: Rect, edge: Style, body: &str, style: Style, clip: Rect) {
    tall_lines(buf, r, edge, &[body.to_owned()], style, clip);
}
fn tall_rows(buf: &mut Buffer, r: Rect, edge: Style) {
    tall_lines(buf, r, edge, &[], edge, r);
}
fn tall_rows_clipped(buf: &mut Buffer, r: Rect, edge: Style, clip: Rect) {
    tall_lines(buf, r, edge, &[], edge, clip);
}

#[allow(clippy::too_many_arguments)]
fn button(
    buf: &mut Buffer,
    r: Rect,
    label: &str,
    variant: &str,
    focused: bool,
    disabled: bool,
    p: &Paint,
    clip: Rect,
) {
    let base = match variant {
        "primary" => p.accent,
        "error" => p.error,
        _ => p.panel,
    };
    let bg = if focused { p.accent } else { base };
    let fg = if disabled {
        p.muted
    } else if variant == "default" && !focused {
        p.text
    } else {
        p.background
    };
    let face = Style::default().fg(fg).bg(bg).add_modifier(Modifier::BOLD);
    let rim = Style::default().fg(p.border).bg(bg);
    let w = usize::from(r.width);
    let lw = label.width();
    let left = w.saturating_sub(lw) / 2;
    for i in 0..r.height {
        let y = r.y + i;
        if y < clip.top() || y >= clip.bottom() {
            continue;
        }
        let (text, style) = if i == 0 {
            ("▔".repeat(w), rim)
        } else if i == r.height - 1 {
            ("▁".repeat(w), rim)
        } else if i == r.height / 2 {
            (
                format!(
                    "{}{}{}",
                    " ".repeat(left),
                    clip_str(label, w),
                    " ".repeat(w.saturating_sub(left + lw))
                ),
                face,
            )
        } else {
            (" ".repeat(w), face)
        };
        buf.set_stringn(r.x, y, &text, w, style);
    }
}
fn clip_str(text: &str, cells: usize) -> String {
    clip(text, cells)
}

/// Textual's ScrollBarRender.render_bar (vertical) in the scrollbar's own region: its rows
/// are the window, its width Textual's scrollbar-size.
fn scrollbar(buf: &mut Buffer, at: Rect, scroll_y: u16, max_scroll_y: u16, p: &Paint) {
    const BARS: [&str; 8] = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", " "];
    let content = at;
    let size = f64::from(content.height);
    let window = size;
    let virtual_size = size + f64::from(max_scroll_y);
    let x = content.x;
    let back = p.panel;
    let bar = p.accent;
    if size <= 0.0 {
        return;
    }
    let thumb = (window / (virtual_size / size)).max(1.0);
    let position = (size - thumb) * (f64::from(scroll_y) / (virtual_size - window));
    let start = (position * 8.0) as i64;
    let end = start + (thumb * 8.0).ceil() as i64;
    let (start_index, start_bar) = (start.max(0) / 8, start.max(0) % 8);
    let (end_index, end_bar) = (end.max(0) / 8, end.max(0) % 8);
    for row in 0..i64::from(content.height) {
        let y = content.y + row as u16;
        let (symbol, style) = if row >= start_index && row < end_index {
            (" ", Style::default().bg(bar))
        } else {
            (" ", Style::default().bg(back))
        };
        let (symbol, style) = if row == start_index && BARS[(7 - start_bar) as usize] != " " {
            (
                BARS[(7 - start_bar) as usize],
                Style::default().fg(bar).bg(back),
            )
        } else if row == end_index && BARS[(7 - end_bar) as usize] != " " {
            (
                BARS[(7 - end_bar) as usize],
                Style::default()
                    .fg(bar)
                    .bg(back)
                    .add_modifier(Modifier::REVERSED),
            )
        } else {
            (symbol, style)
        };
        for dx in 0..content.width.max(1) {
            if x + dx < buf.area.right() {
                buf[(x + dx, y)].set_symbol(symbol).set_style(style);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn month() -> Value {
        json!({"version": 1, "kind": "calendar_dialog", "screen": "calendar", "size": [140, 45],
            "dock": null, "box": [3, 2, 134, 41], "year": 2026, "month": 11,
            "grid": {"region": [6, 4, 94, 36], "lines": [[{"text": "NOVEMBER 2026", "style": "bold #ba3c5b"}]]},
            "side": {"region": [100, 4, 34, 36], "lines": [[{"text": "JOBS", "style": "bold #ba3c5b"}]]},
            "hints": {"region": [6, 40, 128, 1], "lines": [[{"text": "q close", "style": "dim #e0e0e0"}]]}})
    }

    #[test]
    fn a_month_click_is_sent_in_the_grids_own_cells_and_keys_are_the_screens() {
        let mut v = View {
            size: [140, 45],
            ..View::default()
        };
        v.open();
        assert_eq!(v.take_intent(), Some(Intent::Open { size: [140, 45] }));
        v.observe(Update::Snapshot(
            Snapshot::parse(&month()).unwrap().map(Box::new),
        ));
        let mut t = ratatui::Terminal::new(ratatui::backend::TestBackend::new(140, 45)).unwrap();
        let theme = crate::theme::builtin_themes().remove(0);
        t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        let buf = t.backend().buffer().clone();
        let row: String = (6..19).map(|x| buf[(x, 4)].symbol().to_owned()).collect();
        assert_eq!(row, "NOVEMBER 2026");
        assert_eq!(buf[(6, 4)].style().fg, Some(Color::Rgb(0xba, 0x3c, 0x5b)));
        assert_eq!(buf[(100, 10)].symbol(), "│");
        v.mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 6 + 52,
            row: 4 + 6,
            modifiers: KeyModifiers::NONE,
        });
        match v.take_intent() {
            Some(Intent::Act { action, .. }) => assert_eq!(action, json!({"grid_click": [52, 6]})),
            other => panic!("{other:?}"),
        }
        v.observe(Update::Snapshot(
            Snapshot::parse(&month()).unwrap().map(Box::new),
        ));
        v.key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
        assert!(
            matches!(v.take_intent(), Some(Intent::Act { action, .. }) if action == json!({"key": "right"}))
        );
        let mut closed = month();
        closed["screen"] = json!("closed");
        v.observe(Update::Snapshot(
            Snapshot::parse(&closed).unwrap().map(Box::new),
        ));
        assert!(!v.owns_input());
    }

    #[test]
    fn the_day_list_moves_its_highlight_here_and_picks_in_python() {
        let day = json!({"version": 1, "kind": "calendar_dialog", "screen": "day", "size": [140, 45],
            "dock": {"region": [32, 24, 76, 3], "label": "Dock to side"}, "box": [29, 14, 82, 16],
            "content": [32, 16, 76, 12], "scroll_y": 0, "max_scroll_y": 0, "focused": "day-list",
            "items": [
                {"kind": "text", "id": "day-title", "text": "FRIDAY 13 NOVEMBER 2026", "classes": [], "region": [32, 16, 76, 1]},
                {"kind": "options", "id": "day-list", "highlighted": 1, "region": [32, 18, 76, 5], "options": [
                    {"id": "a", "disabled": false, "lines": [[{"text": "• alpha", "style": ""}]]},
                    {"id": null, "disabled": true, "lines": [[{"text": "nothing", "style": ""}]]},
                    {"id": "new", "disabled": false, "lines": [[{"text": "• new job on this day…", "style": ""}]]}]},
                {"kind": "button", "id": "dialog-swap", "label": "Dock to side", "variant": "default", "region": [32, 24, 76, 3]}]});
        let mut v = View {
            size: [140, 45],
            ..View::default()
        };
        v.open();
        v.take_intent();
        v.observe(Update::Snapshot(
            Snapshot::parse(&day).unwrap().map(Box::new),
        ));
        assert_eq!(
            v.stops(),
            vec!["day-list".to_owned(), "dialog-swap".to_owned()]
        );
        // The disabled row is skipped both ways.
        v.highlight = Some(0);
        v.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(v.highlight, Some(2));
        v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(matches!(v.take_intent(),
            Some(Intent::Act { action, .. }) if action == json!({"option": "day-list", "index": 2})));
    }
}
