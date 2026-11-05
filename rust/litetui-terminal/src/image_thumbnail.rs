//! Bounded RGBA data from the reviewed PNG, rendered without terminal graphics negotiation.
use super::Review;
use crate::{theme::Theme, work_lifecycle, work_protocol::Identity};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Style},
    widgets::{Block, Clear},
};
use serde_json::Value;
use std::io;

#[derive(Clone, Debug, PartialEq)]
pub struct Thumbnail {
    width: usize,
    height: usize,
    pixels: Vec<u8>,
}

impl Thumbnail {
    pub fn parse(
        value: &Value,
        id: &Identity,
        instance: &str,
        review: &Review,
    ) -> io::Result<Self> {
        work_lifecycle::keys(
            value,
            &[
                "kind",
                "version",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "token",
                "png_sha256",
                "width",
                "height",
                "rgba",
            ],
        )?;
        let bad = || io::Error::other("Image preview differs from the selected attachment");
        if value["version"].as_u64() != Some(1)
            || value["kind"] != "image_preview"
            || value["session_id"] != id.session
            || value["agent_id"] != id.agent
            || value["backend"] != id.backend
            || value["model"] != id.model
            || value["work_instance"] != instance
            || value["token"] != review.token()
            || value["png_sha256"] != review.wire()["png_sha256"]
        {
            return Err(bad());
        }
        let width = value["width"]
            .as_u64()
            .filter(|w| (1..=64).contains(w))
            .ok_or_else(bad)? as usize;
        let height = value["height"]
            .as_u64()
            .filter(|h| (1..=40).contains(h))
            .ok_or_else(bad)? as usize;
        let count = width * height * 4;
        let encoded = value["rgba"]
            .as_str()
            .filter(|s| s.len() == 4 * count.div_ceil(3))
            .ok_or_else(bad)?;
        let pixels = STANDARD.decode(encoded).map_err(|_| bad())?;
        if pixels.len() != count {
            return Err(bad());
        }
        Ok(Self {
            width,
            height,
            pixels,
        })
    }

    fn color(&self, x: usize, y: usize, background: Color) -> Color {
        let index = 4 * (y * self.width + x);
        let rgba = &self.pixels[index..index + 4];
        match rgba[3] {
            0 => background,
            255 => Color::Rgb(rgba[0], rgba[1], rgba[2]),
            alpha => {
                let Color::Rgb(r, g, b) = background else {
                    return Color::Rgb(rgba[0], rgba[1], rgba[2]);
                };
                let a = u16::from(alpha);
                let blend = |c: u8, bg: u8| {
                    ((u16::from(c) * a + u16::from(bg) * (255 - a) + 127) / 255) as u8
                };
                Color::Rgb(blend(rgba[0], r), blend(rgba[1], g), blend(rgba[2], b))
            }
        }
    }

    fn shade(top: Color, bottom: Color) -> &'static str {
        let luma = |color| match color {
            Color::Rgb(r, g, b) => {
                (54 * u32::from(r) + 183 * u32::from(g) + 19 * u32::from(b)) / 256
            }
            _ => 0,
        };
        let brightness = (luma(top) + luma(bottom)) / 2;
        let shades = [" ", "·", ":", "-", "=", "+", "*", "#", "%", "█"];
        shades[(brightness * (shades.len() as u32 - 1)).div_ceil(255) as usize]
    }

    pub(super) fn draw(&self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let width = area.width.min(78);
        let height = area.height.min(28);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let block = Block::bordered()
            .title(" Image preview · Esc returns to review ")
            .style(Style::default().fg(theme.title).bg(theme.panel));
        let inner = block.inner(rect);
        frame.render_widget(Clear, rect);
        frame.render_widget(block, rect);
        if inner.is_empty() {
            return;
        }
        // Two pixels share a terminal cell. Scale by one factor on both axes.
        let max_w = usize::from(inner.width);
        let max_h = 2 * usize::from(inner.height);
        let (w, h) = if max_w * self.height <= max_h * self.width {
            (max_w, (max_w * self.height / self.width).max(1))
        } else {
            ((max_h * self.width / self.height).max(1), max_h)
        };
        let x0 = inner.x + (inner.width - w as u16) / 2;
        let y0 = inner.y + (inner.height - h.div_ceil(2) as u16) / 2;
        let monochrome = crossterm::style::Colored::ansi_color_disabled_memoized();
        let buf = frame.buffer_mut();
        for y in (0..h).step_by(2) {
            for x in 0..w {
                let sx = x * self.width / w;
                let top = self.color(sx, y * self.height / h, theme.panel);
                let bottom = if y + 1 < h {
                    self.color(sx, (y + 1) * self.height / h, theme.panel)
                } else {
                    theme.panel
                };
                let cell = &mut buf[(x0 + x as u16, y0 + (y / 2) as u16)];
                if monochrome {
                    cell.set_symbol(Self::shade(top, bottom));
                } else {
                    cell.set_symbol("▀").set_fg(top).set_bg(bottom);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn monochrome_preserves_light_and_dark_pixels() {
        let black = Color::Rgb(0, 0, 0);
        let white = Color::Rgb(255, 255, 255);
        assert_eq!(Thumbnail::shade(black, black), " ");
        assert_eq!(Thumbnail::shade(white, white), "█");
        assert_eq!(Thumbnail::shade(white, black), "+");
    }

    #[test]
    fn thumbnail_is_bound_to_the_review_and_has_exact_bounded_pixel_data() {
        let id = Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "model".into(),
        };
        let review = Review(json!({"token": "image-A", "png_sha256": "digest-A"}));
        let mut wire = id.request();
        wire.as_object_mut().unwrap().extend(
            json!({"kind": "image_preview", "work_instance": "instance", "token": "image-A",
                   "png_sha256": "digest-A", "width": 1, "height": 1, "rgba": "ADJkgA=="})
            .as_object()
            .unwrap()
            .clone(),
        );
        let parse = |reply: &Value| Thumbnail::parse(reply, &id, "instance", &review);
        let thumbnail = parse(&wire).unwrap();
        assert_eq!(
            thumbnail.color(0, 0, Color::Rgb(100, 50, 0)),
            Color::Rgb(50, 50, 50)
        );
        for (key, bad) in [
            ("width", json!(65)),
            ("height", json!(0)),
            ("height", json!(41)),
            ("rgba", json!("AA==")),
            ("rgba", json!("AAAAAAAA")),
            ("rgba", json!("!!!!====")),
            ("token", json!("image-B")),
            ("png_sha256", json!("digest-B")),
            ("work_instance", json!("other")),
            ("session_id", json!("other")),
            ("unexpected", json!("extra")),
        ] {
            let mut altered = wire.clone();
            altered[key] = bad;
            assert!(parse(&altered).is_err(), "accepted invalid {key}");
        }
    }
}
