//! Non-modal slash completion presentation. The producer owns catalog authority:
//! primary command names, alias/collision deduplication and palette/skill order.
//! This module neither discovers skills nor dispatches commands.
use ratatui::layout::{Position, Rect};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompletionKind {
    Command,
    Skill,
}
#[derive(Clone, Debug)]
pub struct Completion {
    pub name: String,
    pub description: String,
    pub kind: CompletionKind,
}
#[derive(Default, Debug)]
pub struct CompletionPicker {
    matches: Vec<Completion>,
    selected: usize,
    view: Option<(Rect, usize)>,
    pressed: Option<usize>,
}
impl CompletionPicker {
    /// Candidates must already be in the producer's source sort order. A stable
    /// prefix-first ordering preserves that order within each quality group.
    pub fn refilter(&mut self, candidates: &[Completion], input: &str) {
        self.dismiss();
        let Some(fragment) = input.strip_prefix('/') else {
            return;
        };
        if input.contains(' ') {
            return;
        }
        let want = fragment.to_lowercase();
        self.matches = candidates
            .iter()
            .filter(|candidate| candidate.name.to_lowercase().contains(&want))
            .cloned()
            .collect();
        self.matches
            .sort_by_key(|candidate| !candidate.name.to_lowercase().starts_with(&want));
        self.matches.truncate(200);
    }
    pub fn is_open(&self) -> bool {
        !self.matches.is_empty()
    }
    pub fn matches(&self) -> &[Completion] {
        &self.matches
    }
    pub fn selected(&self) -> usize {
        self.selected
    }
    pub fn current(&self) -> Option<&Completion> {
        self.matches.get(self.selected)
    }
    pub fn move_by(&mut self, delta: isize) {
        if self.is_open() {
            self.hide();
            self.selected = self
                .selected
                .saturating_add_signed(delta)
                .min(self.matches.len() - 1);
        }
    }
    /// Only rows actually rendered may be clicked; borders are excluded.
    pub fn record_view(&mut self, rows: Rect, first: usize) {
        if self.view != Some((rows, first)) {
            self.pressed = None;
        }
        self.view = Some((rows, first));
    }
    pub fn hide(&mut self) {
        self.view = None;
        self.pressed = None;
    }
    fn hit(&self, x: u16, y: u16) -> Option<usize> {
        let (rows, first) = self.view?;
        if !rows.contains(Position::new(x, y)) {
            return None;
        }
        let index = first + usize::from(y - rows.y);
        (index < self.matches.len()).then_some(index)
    }
    pub fn pointer_down(&mut self, x: u16, y: u16) {
        self.pressed = self.hit(x, y);
    }
    pub fn pointer_up(&mut self, x: u16, y: u16) -> bool {
        let pressed = self.pressed.take();
        if let Some(index) = self.hit(x, y)
            && pressed == Some(index)
        {
            self.selected = index;
            return true;
        }
        false
    }
    pub fn dismiss(&mut self) {
        self.hide();
        self.matches.clear();
        self.selected = 0;
    }
}
