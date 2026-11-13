//! Python's reply Markdown (src/litetui/native_markdown.py): the lines Python's AnswerBody
//! draws for an answer at a width (Rich's Markdown, as textfmt._markdown_to_text renders
//! it), each line a list of styled runs. The transcript asks for them per card and draws
//! them in place of the raw answer text.
use ratatui::style::{Color, Modifier, Style};
use serde_json::{Value, json};
use std::io;

pub const OPERATION: &str = "gui.markdown.native_render";
const MAX_TEXT: usize = 256 * 1024;
const MAX_LINES: usize = 20_000;
const MAX_RUNS: usize = 4096;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Run {
    pub text: String,
    pub fg: Option<Color>,
    pub bg: Option<Color>,
    pub modifiers: Modifier,
}
impl Run {
    /// This run's style over the card's own foreground and background.
    pub fn style(&self, fg: Color, bg: Color) -> Style {
        Style::default()
            .fg(self.fg.unwrap_or(fg))
            .bg(self.bg.unwrap_or(bg))
            .add_modifier(self.modifiers)
    }
}
pub type Lines = Vec<Vec<Run>>;

pub fn request(text: &str, width: u16) -> io::Result<Value> {
    if text.len() > MAX_TEXT || width == 0 {
        return Err(io::Error::other("Answer is too long to render as Markdown"));
    }
    Ok(json!({"type": OPERATION, "version": 1, "text": text, "width": width}))
}

fn color(v: Option<&Value>) -> io::Result<Option<Color>> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "Invalid Markdown colour");
    match v {
        None => Ok(None),
        Some(Value::String(s)) if s.len() == 7 && s.starts_with('#') => {
            let n = u32::from_str_radix(&s[1..], 16).map_err(|_| bad())?;
            Ok(Some(Color::Rgb((n >> 16) as u8, (n >> 8) as u8, n as u8)))
        }
        Some(_) => Err(bad()),
    }
}

/// The render's lines, for the width that was asked.
pub fn parse(result: &Value, width: u16) -> io::Result<Lines> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "Invalid Markdown render");
    if result["version"] != 1 || result["width"] != width {
        return Err(bad());
    }
    let lines = result["lines"]
        .as_array()
        .filter(|l| l.len() <= MAX_LINES)
        .ok_or_else(bad)?;
    lines
        .iter()
        .map(|line| {
            let runs = line
                .as_array()
                .filter(|r| r.len() <= MAX_RUNS)
                .ok_or_else(bad)?;
            runs.iter()
                .map(|run| {
                    let (Some(text), Some(style)) =
                        (run.get(0).and_then(Value::as_str), run.get(1))
                    else {
                        return Err(bad());
                    };
                    let style = style.as_object().ok_or_else(bad)?;
                    let mut modifiers = Modifier::empty();
                    for (name, flag) in [
                        ("bold", Modifier::BOLD),
                        ("dim", Modifier::DIM),
                        ("italic", Modifier::ITALIC),
                        ("underline", Modifier::UNDERLINED),
                        ("strike", Modifier::CROSSED_OUT),
                        ("reverse", Modifier::REVERSED),
                    ] {
                        if style.get(name) == Some(&Value::Bool(true)) {
                            modifiers |= flag;
                        }
                    }
                    Ok(Run {
                        text: text.to_owned(),
                        fg: color(style.get("fg"))?,
                        bg: color(style.get("bg"))?,
                        modifiers,
                    })
                })
                .collect::<io::Result<Vec<_>>>()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_reads_runs_and_refuses_another_width() {
        let v = json!({"version": 1, "width": 40, "lines": [
            [["• ", {"bold": true}], ["item", {}]],
            [],
            [[" x = 1", {"fg": "#f8f8f2", "bg": "#272822"}]]
        ]});
        let lines = parse(&v, 40).unwrap();
        assert_eq!(lines.len(), 3);
        assert_eq!(lines[0][0].modifiers, Modifier::BOLD);
        assert_eq!(lines[2][0].bg, Some(Color::Rgb(0x27, 0x28, 0x22)));
        assert!(parse(&v, 41).is_err());
        assert!(
            parse(
                &json!({"version": 1, "width": 40, "lines": [[["x", {"fg": "red"}]]]}),
                40
            )
            .is_err()
        );
    }
}
