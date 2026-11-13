//! Semantic presentation colors. The preview catalog is a public-source fixture;
//! live source snapshots supply resolved colors, including custom overrides.
use ratatui::style::Color;
use std::collections::BTreeMap;
use std::sync::Arc;

#[derive(Clone, Debug)]
pub struct Theme {
    pub name: String,
    pub background: Color,
    pub surface: Color,
    pub primary: Color,
    pub panel: Color,
    pub tool_background: Color,
    pub foreground: Color,
    pub title: Color,
    pub assistant_border: Color,
    pub thinking_text: Color,
    pub thinking_border: Color,
    pub tool_text: Color,
    pub error: Color,
    pub footer_foreground: Color,
    pub footer_background: Color,
    /// The running theme's resolved CSS variables (Python's `App.get_css_variables`), which
    /// LiteTUI's stylesheet paints every widget from. Empty for the offline preview catalog.
    pub css: Arc<BTreeMap<String, String>>,
}

/// A CSS colour value as Textual parses it: RGB, alpha, and whether it is `auto` (the
/// contrast text of whatever it is drawn over).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Css {
    pub rgb: (u8, u8, u8),
    pub alpha: f32,
    pub auto: bool,
}

impl Css {
    /// `#RGB`, `#RGBA`, `#RRGGBB`, `#RRGGBBAA`, or `auto` / `auto N%`.
    pub fn parse(text: &str) -> Option<Self> {
        let text = text.trim();
        if let Some(rest) = text.strip_prefix("auto") {
            let rest = rest.trim();
            let alpha = if rest.is_empty() {
                1.0
            } else {
                rest.strip_suffix('%')?.trim().parse::<f32>().ok()? / 100.0
            };
            return Some(Self {
                rgb: (0, 0, 0),
                alpha: alpha.clamp(0.0, 1.0),
                auto: true,
            });
        }
        let h = text.strip_prefix('#')?;
        let digit = |i: usize, n: usize| u8::from_str_radix(h.get(i..i + n)?, 16).ok();
        let (r, g, b, a) = match h.len() {
            3 | 4 => {
                let one = |i| digit(i, 1).map(|v| v * 17);
                (one(0)?, one(1)?, one(2)?, if h.len() == 4 { one(3)? } else { 255 })
            }
            6 | 8 => (
                digit(0, 2)?,
                digit(2, 2)?,
                digit(4, 2)?,
                if h.len() == 8 { digit(6, 2)? } else { 255 },
            ),
            _ => return None,
        };
        Some(Self {
            rgb: (r, g, b),
            alpha: f32::from(a) / 255.0,
            auto: false,
        })
    }
    /// This colour composited over `base`, as Textual's `base + colour` does
    /// (`Color.blend`, truncating; `auto` is the base's contrast text, white or black).
    pub fn over(self, base: Color) -> Color {
        let Color::Rgb(br, bg, bb) = base else {
            let (r, g, b) = self.rgb;
            return Color::Rgb(r, g, b);
        };
        let (r, g, b) = if self.auto {
            let brightness = (299.0 * f32::from(br) + 587.0 * f32::from(bg) + 114.0 * f32::from(bb))
                / 1000.0
                / 255.0;
            if brightness < 0.5 { (255, 255, 255) } else { (0, 0, 0) }
        } else {
            self.rgb
        };
        blend(base, Color::Rgb(r, g, b), self.alpha)
    }
}

/// Textual's `Color.blend(destination, factor)`: `a + (b - a) * factor`, truncated.
pub fn blend(from: Color, to: Color, factor: f32) -> Color {
    match (from, to) {
        (Color::Rgb(ar, ag, ab), Color::Rgb(br, bg, bb)) => {
            if factor <= 0.0 {
                return from;
            }
            if factor >= 1.0 {
                return to;
            }
            let mix = |a: u8, b: u8| (f32::from(a) + (f32::from(b) - f32::from(a)) * factor) as u8;
            Color::Rgb(mix(ar, br), mix(ag, bg), mix(ab, bb))
        }
        _ => to,
    }
}

impl Theme {
    /// The CSS variable `$name` (without the `$`), parsed.
    pub fn css_value(&self, name: &str) -> Option<Css> {
        self.css.get(name).and_then(|v| Css::parse(v))
    }
    /// `$name` drawn over `base` (alpha and `auto` resolved as Textual does), or `fallback`
    /// when this theme carries no such variable (the offline preview catalog).
    pub fn css_over(&self, name: &str, base: Color, fallback: Color) -> Color {
        self.css_value(name).map_or(fallback, |c| c.over(base))
    }
    /// `$name N%` (a CSS declaration's own alpha) drawn over `base`.
    pub fn css_alpha(&self, name: &str, percent: f32, base: Color, fallback: Color) -> Color {
        self.css_value(name).map_or(fallback, |c| {
            Css {
                alpha: c.alpha * percent / 100.0,
                ..c
            }
            .over(base)
        })
    }
    /// An opaque `$name` (a background, say), or `fallback`.
    pub fn var(&self, name: &str, fallback: Color) -> Color {
        self.css_over(name, fallback, fallback)
    }

    // LiteTUI's stylesheet roles (app.py CSS, input_controls.py, Textual's defaults), each
    // from the variable the CSS names; the preview catalog (no CSS) keeps the old mapping.

    /// `Screen { background: $surface-darken-1 }` (and #chat-log over it).
    pub fn screen(&self) -> Color {
        self.var("surface-darken-1", self.background)
    }
    /// `$foreground`, the text colour of the screen and most widgets.
    pub fn text(&self) -> Color {
        self.var("foreground", self.foreground)
    }
    /// `$text-muted` (auto 60%: the contrast text of `bg` at 60%), over `bg`.
    pub fn muted_on(&self, bg: Color) -> Color {
        self.css_over("text-muted", bg, self.thinking_text)
    }
    /// `$text-disabled` over `bg` (an Input's placeholder).
    pub fn disabled_on(&self, bg: Color) -> Color {
        self.css_over("text-disabled", bg, self.title)
    }
    /// Textual's Header: `background: $panel; color: $foreground`.
    pub fn header(&self) -> Color {
        self.var("panel", self.surface)
    }
    /// `$footer-background`, the ContextFooter rows and the prompt's mic/pause chips.
    pub fn footer_bg(&self) -> Color {
        self.var("footer-background", self.footer_background)
    }
    /// The focused PromptInput: `background: $surface` with `background-tint: $foreground 5%`.
    pub fn input(&self) -> Color {
        let surface = self.var("surface", self.surface);
        self.css_alpha("foreground", 5.0, surface, surface)
    }
    /// The focused PromptInput's `border: tall $border`.
    pub fn input_border(&self) -> Color {
        self.var("border", self.thinking_border)
    }
    /// `$surface`.
    pub fn surface_bg(&self) -> Color {
        self.var("surface", self.surface)
    }
    /// `$accent` (the footer's meters toggle).
    pub fn accent(&self) -> Color {
        self.var("accent", self.title)
    }
    /// A theme's explicit `footer-foreground`, which recolours the footer's neutral fields
    /// (app.py `neutral_style`); None for a theme that leaves them in their own colours.
    pub fn footer_neutral(&self) -> Option<Color> {
        self.css.get("litetui-footer-neutral").and_then(|v| Css::parse(v)).map(|c| {
            let (r, g, b) = c.rgb;
            Color::Rgb(r, g, b)
        })
    }
}

/// A Rich style spec as Textual paints it over `base` (fg/bg defaults): Textual's
/// compositor never emits DIM, it blends the foreground toward the background by
/// DIM_FACTOR 0.66 (`textual.filter.dim_color`, truncating) and drops the attribute.
pub fn textual_style(spec: &str, base: ratatui::style::Style) -> ratatui::style::Style {
    textual_dim(crate::calendar_dialog::rich_style(spec, base))
}
/// Textual's dim, resolved (see `textual_style`).
pub fn textual_dim(style: ratatui::style::Style) -> ratatui::style::Style {
    use ratatui::style::Modifier;
    match (style.add_modifier.contains(Modifier::DIM), style.fg, style.bg) {
        (true, Some(Color::Rgb(fr, fg, fb)), Some(Color::Rgb(br, bg, bb))) => {
            let mix = |b: u8, f: u8| (f32::from(b) + (f32::from(f) - f32::from(b)) * 0.66) as u8;
            style
                .fg(Color::Rgb(mix(br, fr), mix(bg, fg), mix(bb, fb)))
                .remove_modifier(Modifier::DIM)
        }
        _ => style,
    }
}

include!("theme_catalog.rs");

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn css_values_composite_as_textual_does() {
        // textual-dark: $text-muted "auto 60%" over $surface-darken-1 #0d0d0d is #9e9e9e
        // (white at 0.6, truncated), the colour Python's system lines carry.
        let muted = Css::parse("auto 60%").unwrap();
        assert_eq!(muted.over(Color::Rgb(13, 13, 13)), Color::Rgb(158, 158, 158));
        // $block-cursor-blurred-background #0178D44C over $panel-darken-1 #141f27 is #0e395a.
        let cursor = Css::parse("#0178D44C").unwrap();
        assert_eq!(cursor.over(Color::Rgb(20, 31, 39)), Color::Rgb(14, 57, 90));
        assert_eq!(Css::parse("#fff").unwrap().rgb, (255, 255, 255));
        assert_eq!(Css::parse("bold"), None);
    }
}
