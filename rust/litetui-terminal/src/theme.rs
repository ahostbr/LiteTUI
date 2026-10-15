//! Semantic presentation colors. The preview catalog is a public-source fixture;
//! a future runtime adapter supplies resolved colors, including custom overrides.
use ratatui::style::Color;

#[derive(Clone, Debug)]
pub struct Theme {
    pub name: String,
    pub background: Color,
    pub surface: Color,
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
}

include!("theme_catalog.rs");
