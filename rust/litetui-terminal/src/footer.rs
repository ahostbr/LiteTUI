//! Two-row footer projection and keyboard ownership. Labels/order are supplied
//! by the state owner; an activation is an intent, never a policy transition.
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::rendered_width;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FieldKey {
    Percent,
    Context,
    Seat,
    Conversation,
    RestoredInput,
    Model,
    Thinking,
    Cache,
    Throughput,
    Background,
    Agents,
    Authority,
    Plan,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FooterAction {
    Authority,
    Plan,
    Thinking,
    Background,
    Agents,
}
impl FieldKey {
    pub fn action(self) -> Option<FooterAction> {
        Some(match self {
            Self::Authority => FooterAction::Authority,
            Self::Plan => FooterAction::Plan,
            Self::Thinking => FooterAction::Thinking,
            Self::Background => FooterAction::Background,
            Self::Agents => FooterAction::Agents,
            _ => return None,
        })
    }
    fn permission(self) -> bool {
        matches!(self, Self::Authority | Self::Plan)
    }
}
#[derive(Clone, Debug)]
pub struct FooterField {
    pub key: FieldKey,
    pub text: String,
}
impl FooterField {
    pub fn new(key: FieldKey, text: impl Into<String>) -> Self {
        Self {
            key,
            text: text.into(),
        }
    }
}
#[derive(Debug)]
pub struct FooterRow {
    pub fields: Vec<FooterField>,
    pub separator: &'static str,
    pub overflow: bool,
}
impl FooterRow {
    pub fn text(&self) -> String {
        if self.overflow {
            return "…".into();
        }
        self.fields
            .iter()
            .map(|field| field.text.as_str())
            .collect::<Vec<_>>()
            .join(self.separator)
    }
    fn width(&self) -> usize {
        self.fields
            .iter()
            .map(|field| rendered_width(&field.text))
            .sum::<usize>()
            + self.separator.width() * self.fields.len().saturating_sub(1)
    }
    fn finish(&mut self, width: usize) {
        if self.width() > width {
            self.fields.clear();
            self.overflow = width > 0;
        }
    }
}
#[derive(Debug)]
pub struct FooterLayout {
    pub status: FooterRow,
    pub permission: FooterRow,
    /// Source global configured order, filtered by the final two-row rendering.
    pub actions: Vec<FooterAction>,
}
impl FooterLayout {
    /// Caller provides normalized source order and only currently applicable
    /// facts. Unknown authority is represented by ABSENCE, never a default level.
    pub fn new(fields: &[FooterField], width: usize) -> Self {
        let mut status = FooterRow {
            fields: vec![],
            separator: "  ·  ",
            overflow: false,
        };
        let mut permission = FooterRow {
            fields: vec![],
            separator: "  ·  ",
            overflow: false,
        };
        let mut seen = vec![];
        for field in fields {
            if seen.contains(&field.key) {
                continue;
            }
            seen.push(field.key);
            let text: String = field.text.chars().filter(|ch| !ch.is_control()).collect();
            if text.trim().is_empty() || rendered_width(&text) == 0 {
                continue;
            }
            let row = if field.key.permission() {
                &mut permission
            } else {
                &mut status
            };
            row.fields.push(FooterField::new(field.key, text));
        }
        if status.width() > width {
            status.separator = " · ";
        }
        for key in [
            FieldKey::Throughput,
            FieldKey::Background,
            FieldKey::Agents,
            FieldKey::Cache,
            FieldKey::Model,
            FieldKey::Conversation,
            FieldKey::Context,
        ] {
            if status.width() <= width {
                break;
            }
            status.fields.retain(|field| field.key != key);
        }
        let excess = status.width().saturating_sub(width);
        if excess > 0
            && let Some(seat) = status
                .fields
                .iter_mut()
                .find(|field| field.key == FieldKey::Seat)
        {
            let target = rendered_width(&seat.text).saturating_sub(excess).max(7);
            if rendered_width(&seat.text) > target {
                let mut used = 0;
                let prefix: String = seat
                    .text
                    .graphemes(true)
                    .take_while(|glyph| {
                        used += glyph.width();
                        used < target
                    })
                    .collect();
                let shortened = format!("{prefix}…");
                // An oversized leading grapheme cannot yield a recognizable
                // prefix. Keep the seat intact, concede thinking, then overflow
                // if needed rather than recovering space by erasing identity.
                if rendered_width(&shortened) >= 7 {
                    seat.text = shortened;
                }
            }
        }
        if status.width() > width {
            status
                .fields
                .retain(|field| field.key != FieldKey::Thinking);
        }
        status.finish(width);
        permission.finish(width);
        let mut actions = vec![];
        for field in fields {
            if let Some(action) = field.key.action()
                && !actions.contains(&action)
                && status
                    .fields
                    .iter()
                    .chain(&permission.fields)
                    .any(|shown| shown.key == field.key)
            {
                actions.push(action);
            }
        }
        Self {
            status,
            permission,
            actions,
        }
    }
}

#[derive(Default, Debug)]
pub struct FooterFocus {
    visible: Vec<FooterAction>,
    selected: Option<FooterAction>,
    stale_activation: bool,
}
impl FooterFocus {
    pub fn update_visible(&mut self, visible: &[FooterAction]) {
        self.visible = visible.to_vec();
        if self
            .selected
            .is_some_and(|selected| !self.visible.contains(&selected))
        {
            self.selected = None;
            // Do not turn the Enter aimed at a removed chip into a submission.
            self.stale_activation = true;
        }
    }
    pub fn selected(&self) -> Option<FooterAction> {
        self.selected
    }
    pub fn owns_enter(&self) -> bool {
        self.selected.is_some() || self.stale_activation
    }
    pub fn enter(&mut self) -> bool {
        self.stale_activation = false;
        self.selected = self.visible.first().copied();
        self.selected.is_some()
    }
    pub fn leave(&mut self) {
        self.selected = None;
        self.stale_activation = false;
    }
    pub fn move_by(&mut self, delta: isize) {
        if self.visible.is_empty() {
            self.leave();
            return;
        }
        let index = self
            .selected
            .and_then(|key| self.visible.iter().position(|item| *item == key));
        let next = match index {
            Some(index) => {
                (index + delta.rem_euclid(self.visible.len() as isize) as usize)
                    % self.visible.len()
            }
            None if delta < 0 => self.visible.len() - 1,
            None => 0,
        };
        self.selected = Some(self.visible[next]);
        self.stale_activation = false;
    }
    /// Returns whether this Enter was owned, plus a currently visible intent.
    pub fn activate(&mut self) -> (bool, Option<FooterAction>) {
        if std::mem::take(&mut self.stale_activation) {
            return (true, None);
        }
        (
            self.selected.is_some(),
            self.selected.filter(|key| self.visible.contains(key)),
        )
    }
}
