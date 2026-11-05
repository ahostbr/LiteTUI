//! The F4 custom-theme colour picker feeds the existing reviewed draft and never
//! saves: render, key and mouse mapping, and the review/confirm path it joins.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use litetui_terminal::color_picker::{
    GRID_H, GRID_W, cell_to_hue, cell_to_sv, hex_to_hsv, hsv_to_hex, hsv_to_rgb, preset_colors,
};
use litetui_terminal::theme_controls::{
    self as themes, Change, Identity, Intent, Review, Snapshot, Update, View,
};
use ratatui::{Terminal, backend::TestBackend, buffer::Buffer, style::Color};
use serde_json::{Value, json};

const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
const START: &str = "#123456";

fn identity() -> Identity {
    Identity {
        session: SESSION.into(),
        agent: AGENT.into(),
        backend: "custom".into(),
        model: "test".into(),
    }
}
fn token_rows(hex: &str) -> Value {
    json!(themes::TOKENS.iter().enumerate().map(|(i,k)|json!({"key":k,"required":i<10,"raw":if i<13 {json!(hex)}else{Value::Null},"resolved":if i<13{json!(hex)}else{Value::Null},"source":if i<13{"explicit"}else{"omitted"}})).collect::<Vec<_>>())
}
fn palette() -> Value {
    let mut v = json!({});
    for (i, k) in themes::COLORS.iter().enumerate() {
        v[*k] = json!([i, 30, 200]);
    }
    v
}
fn row(id: char, name: &str) -> Value {
    json!({"row_id":id.to_string().repeat(64),"name":name,"definition":id.to_string().repeat(64),"source":"custom","selectable":true,"reason":null,"palette":palette(),"tokens":token_rows(START)})
}
fn data() -> Value {
    json!({"version":1,"kind":"theme_snapshot","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":"c".repeat(64),"scope":"device","apply_timing":"immediate","revisions":{"global":"d".repeat(64),"conversation":"absent","agent":"e".repeat(64)},"selection":{"saved_name":"Old","saved_definition":"a".repeat(64),"requested_name":"Old","runtime_name":"Old","effective_name":"Old","effective_row_id":"a".repeat(64),"effective_reason":null,"effective_definition":"a".repeat(64)},"eligible":true,"reason":null,"registered_definitions":{"Old":"a".repeat(64),"New":"b".repeat(64)},"rows":[row('a',"Old"),row('b',"New")],"invalid":[]})
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&data(), &identity()).unwrap()
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    }
}
fn theme() -> litetui_terminal::theme::Theme {
    litetui_terminal::theme::builtin_themes().remove(0)
}
fn draw(v: &mut View, w: u16, h: u16) -> Buffer {
    let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
    let theme = theme();
    t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    t.backend().buffer().clone()
}
fn text(b: &Buffer) -> String {
    b.content.iter().map(|c| c.symbol()).collect()
}
/// An editor over the "Old" row (F3), parked on `primary`, picker open and drawn at 80x28.
/// Layout there: panel = whole screen, inner (1,1,78,26): title y=1, field (1,2,48,12),
/// hue y=14, presets y=15, hex row y=16 (swatch x=1..7, hex box x=8), hint y=17.
fn picking() -> View {
    let mut v = View::default();
    v.open();
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Snapshot(Box::new(snapshot())));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::F(3)));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Tab));
    assert_eq!(v.draft_token("primary"), Some(START));
    v.key(key(KeyCode::F(8)));
    draw(&mut v, 80, 28);
    v
}
const FIELD: (u16, u16) = (1, 2);
const HUE_Y: u16 = 14;
const PRESET_Y: u16 = 15;
const HEX_XY: (u16, u16) = (8, 16);

fn click(v: &mut View, x: u16, y: u16) {
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), x, y));
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), x, y));
    draw(v, 80, 28);
}
fn accept(v: &mut View) -> String {
    v.key(key(KeyCode::Enter));
    v.draft_token("primary").unwrap().to_owned()
}

#[test]
fn the_field_is_painted_with_half_blocks_one_marker_and_the_start_colour_under_it() {
    let mut v = picking();
    let b = draw(&mut v, 80, 28);
    let (h, s, val) = hex_to_hsv(START).unwrap();
    let mut markers = 0;
    for tr in 0..GRID_H / 2 {
        for col in 0..GRID_W {
            let cell = &b[(FIELD.0 + col as u16, FIELD.1 + tr as u16)];
            let (st, vt) = cell_to_sv(col as i32, (tr * 2) as i32, 0);
            let (sb, vb) = cell_to_sv(col as i32, (tr * 2 + 1) as i32, 0);
            let [tr_, tg, tb] = hsv_to_rgb(h, st, vt);
            let [br, bg, bb] = hsv_to_rgb(h, sb, vb);
            assert_eq!(cell.fg, Color::Rgb(tr_, tg, tb), "top {col},{tr}");
            assert_eq!(cell.bg, Color::Rgb(br, bg, bb), "bottom {col},{tr}");
            match cell.symbol() {
                "▀" => {}
                "+" => markers += 1,
                other => panic!("unexpected {other:?} at {col},{tr}"),
            }
        }
    }
    assert_eq!(markers, 1);
    // The marker sits on the cell that maps back to the start colour.
    let (mc, mr) = litetui_terminal::color_picker::sv_to_cell(s, val, GRID_W as i32);
    assert_eq!(
        b[(FIELD.0 + mc as u16, FIELD.1 + (mr / 2) as u16)].symbol(),
        "+"
    );
}

#[test]
fn the_hue_bar_swatch_hex_and_theme_presets_are_drawn_beside_the_field() {
    let mut v = picking();
    let b = draw(&mut v, 80, 28);
    let (h, _, _) = hex_to_hsv(START).unwrap();
    let mut selected = 0;
    for col in 0..GRID_W as u16 {
        let cell = &b[(FIELD.0 + col, HUE_Y)];
        let hue = cell_to_hue(i32::from(col), 0);
        let [r, g, bl] = hsv_to_rgb(hue, 1.0, 1.0);
        assert_eq!(cell.fg, Color::Rgb(r, g, bl));
        if cell.symbol() == "▼" {
            selected += 1;
            assert_eq!(
                usize::from(col),
                litetui_terminal::color_picker::hue_to_cell(h, 0)
            );
        }
    }
    assert_eq!(selected, 1);
    // Presets are the CURRENT theme's colours, 2 cells + 1 gap each.
    for (i, (_, hex)) in preset_colors(&theme()).iter().enumerate() {
        let (ph, ps, pv) = hex_to_hsv(hex).unwrap();
        let [r, g, bl] = hsv_to_rgb(ph, ps, pv);
        for dx in 0..2 {
            assert_eq!(
                b[(FIELD.0 + (i * 3) as u16 + dx, PRESET_Y)].bg,
                Color::Rgb(r, g, bl),
                "preset {i}"
            );
        }
    }
    // Live preview swatch and the hex escape hatch.
    let (sr, sg, sb) = (0x12, 0x34, 0x56);
    assert_eq!(b[(FIELD.0, 16)].bg, Color::Rgb(sr, sg, sb));
    assert!(text(&b).contains("Pick primary"));
    assert!(text(&b).contains(START));
}

#[test]
fn picker_chrome_uses_the_current_theme_tokens() {
    let mut v = picking();
    let b = draw(&mut v, 80, 28);
    let theme = theme();
    let title = (0..78u16)
        .map(|dx| &b[(1 + dx, 1)])
        .find(|c| c.symbol() == "P")
        .expect("title row");
    assert_eq!(title.fg, theme.title);
    let hint = (0..78u16)
        .map(|dx| &b[(1 + dx, 17)])
        .find(|c| c.symbol() == "E")
        .expect("hint row");
    assert_eq!(hint.fg, theme.foreground);
    assert_eq!(b[(0, 0)].fg, theme.title, "panel border is the theme's");
}

#[test]
fn arrow_and_page_keys_walk_the_grid_and_enter_fills_only_the_field() {
    let (h0, s0, v0) = hex_to_hsv(START).unwrap();
    let step_s: f64 = 1.0 / (GRID_W as f64 - 1.0);
    let step_v: f64 = 1.0 / (GRID_H as f64 - 1.0);
    for (code, expect) in [
        (KeyCode::Right, hsv_to_hex(h0, (s0 + step_s).min(1.0), v0)),
        (KeyCode::Left, hsv_to_hex(h0, (s0 - step_s).max(0.0), v0)),
        (KeyCode::Up, hsv_to_hex(h0, s0, (v0 + step_v).min(1.0))),
        (KeyCode::Down, hsv_to_hex(h0, s0, (v0 - step_v).max(0.0))),
        (KeyCode::PageUp, hsv_to_hex((h0 + 1.0 / 48.0) % 1.0, s0, v0)),
        (
            KeyCode::PageDown,
            hsv_to_hex((h0 - 1.0f64 / 48.0).rem_euclid(1.0), s0, v0),
        ),
    ] {
        let mut v = picking();
        v.key(key(code));
        let b = draw(&mut v, 80, 28);
        assert!(text(&b).contains(&expect), "{code:?} -> {expect}");
        assert_eq!(
            v.draft_token("primary"),
            Some(START),
            "a walk alone never touches the draft"
        );
        assert_eq!(accept(&mut v), expect, "{code:?}");
    }
}

#[test]
fn a_click_on_the_field_maps_through_the_painted_geometry() {
    let (h0, _, _) = hex_to_hsv(START).unwrap();
    for (col, trow) in [(0u16, 0u16), (10, 3), (47, 11), (24, 6)] {
        let mut v = picking();
        click(&mut v, FIELD.0 + col, FIELD.1 + trow);
        // Each text row is two logical cells; the top one is the click.
        let (s, val) = cell_to_sv(i32::from(col), i32::from(trow) * 2, 0);
        assert_eq!(accept(&mut v), hsv_to_hex(h0, s, val), "{col},{trow}");
    }
}

#[test]
fn a_click_on_the_hue_bar_and_on_a_preset_select_the_colour_under_the_cursor() {
    let (_, s0, v0) = hex_to_hsv(START).unwrap();
    let mut v = picking();
    click(&mut v, FIELD.0 + 30, HUE_Y);
    assert_eq!(
        accept(&mut v),
        hsv_to_hex(cell_to_hue(30, 0), s0, v0),
        "hue 30"
    );
    for (i, (_, hex)) in preset_colors(&theme()).iter().enumerate().take(5) {
        for dx in [0u16, 1] {
            let mut v = picking();
            click(&mut v, FIELD.0 + (i * 3) as u16 + dx, PRESET_Y);
            assert_eq!(&accept(&mut v), hex, "preset {i}+{dx}");
        }
    }
}

#[test]
fn a_press_without_its_release_on_the_same_cell_does_not_act_and_clicks_never_accept() {
    let mut v = picking();
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 20, 5));
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 21, 5));
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 20, 5));
    draw(&mut v, 80, 28);
    assert!(
        text(&draw(&mut v, 80, 28)).contains(START),
        "picker colour unmoved"
    );
    click(&mut v, 20, 5);
    assert_eq!(v.draft_token("primary"), Some(START), "click never fills");
    assert!(text(&draw(&mut v, 80, 28)).contains("Pick primary"));
}

#[test]
fn a_press_and_release_may_straddle_a_frame_but_not_a_resize() {
    let (h0, _, _) = hex_to_hsv(START).unwrap();
    let mut v = picking();
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 20, 5));
    draw(&mut v, 80, 28);
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 20, 5));
    let (s, val) = cell_to_sv(19, 6, 0);
    assert_eq!(accept(&mut v), hsv_to_hex(h0, s, val));
    let mut v = picking();
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 20, 5));
    draw(&mut v, 90, 30);
    draw(&mut v, 80, 28);
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 20, 5));
    assert_eq!(accept(&mut v), START, "a resize disarmed the press");
}

#[test]
fn the_hex_box_is_an_editable_escape_hatch_by_key_paste_and_click() {
    let mut v = picking();
    click(&mut v, HEX_XY.0 + 2, HEX_XY.1);
    for _ in 0..7 {
        v.key(key(KeyCode::Backspace));
    }
    v.paste("#c9a24d");
    let b = draw(&mut v, 80, 28);
    assert_eq!(
        b[(FIELD.0, 16)].bg,
        Color::Rgb(0xc9, 0xa2, 0x4d),
        "live preview"
    );
    assert!(text(&b).contains("#c9a24d"));
    v.paste("not hex");
    assert!(text(&draw(&mut v, 80, 28)).contains("Only # and hex digits"));
    assert_eq!(accept(&mut v), "#c9a24d");
    // Typed junk is refused, not replaced by the grid colour.
    let mut v = picking();
    v.key(key(KeyCode::Tab));
    v.key(key(KeyCode::Backspace));
    v.key(key(KeyCode::Enter));
    let b = draw(&mut v, 80, 28);
    assert!(text(&b).contains("Not a #RRGGBB hex"));
    assert_eq!(v.draft_token("primary"), Some(START));
}

#[test]
fn escape_leaves_the_field_alone_and_a_new_picker_starts_from_the_field() {
    let mut v = picking();
    v.key(key(KeyCode::Up));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Esc));
    let b = draw(&mut v, 80, 28);
    assert_eq!(v.draft_token("primary"), Some(START));
    assert!(text(&b).contains("Custom draft"), "back in the editor");
    v.key(key(KeyCode::F(8)));
    assert!(text(&draw(&mut v, 80, 28)).contains(START));
    // Esc twice more: picker, then editor; the view itself stays open.
    v.key(key(KeyCode::Esc));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Esc));
    draw(&mut v, 80, 28);
    assert!(v.is_open());
    assert!(!v.editing());
}

#[test]
fn f8_on_the_name_field_does_not_open_a_picker() {
    let mut v = View::default();
    v.open();
    v.take_intent();
    v.observe(Update::Snapshot(Box::new(snapshot())));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::F(3)));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::F(8)));
    let b = draw(&mut v, 80, 28);
    assert!(!text(&b).contains("Pick "));
    assert!(v.notice.contains("Tab to a color token"));
}

#[test]
fn a_terminal_too_small_for_the_picker_says_so_and_ignores_keys_but_escape() {
    let mut v = picking();
    let b = draw(&mut v, 42, 18);
    assert!(text(&b).contains("Resize"));
    v.key(key(KeyCode::Up));
    v.key(key(KeyCode::Enter));
    assert_eq!(v.draft_token("primary"), Some(START));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Esc));
    assert!(text(&draw(&mut v, 42, 18)).contains("Custom draft"));
}

#[test]
fn a_narrow_panel_paints_a_narrow_field_and_clicks_map_at_that_width() {
    let mut v = View::default();
    v.open();
    v.take_intent();
    v.observe(Update::Snapshot(Box::new(snapshot())));
    draw(&mut v, 44, 22);
    v.key(key(KeyCode::F(3)));
    draw(&mut v, 44, 22);
    v.key(key(KeyCode::Tab));
    v.key(key(KeyCode::F(8)));
    let b = draw(&mut v, 44, 22);
    // inner = (1,1,42,20): clamp_width(42) = 42 columns; the 43rd is blank.
    let painted = (0..42u16)
        .filter(|dx| matches!(b[(1 + dx, 2)].symbol(), "▀" | "+"))
        .count();
    assert_eq!(painted, 42);
    assert_ne!(b[(1 + 42, 2)].symbol(), "▀");
    let (h0, _, _) = hex_to_hsv(START).unwrap();
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 1 + 41, 2));
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 1 + 41, 2));
    draw(&mut v, 44, 22);
    v.key(key(KeyCode::Enter));
    let (s, val) = cell_to_sv(41, 0, 42);
    assert_eq!(
        v.draft_token("primary"),
        Some(hsv_to_hex(h0, s, val).as_str())
    );
    assert_eq!(s, 1.0, "the last painted column is full saturation");
}

fn custom_review(change: &Change, snapshot: &Snapshot) -> Review {
    let Change::Custom { name, tokens } = change else {
        panic!("custom change")
    };
    let rows = themes::TOKENS
        .iter()
        .enumerate()
        .map(|(i, k)| {
            let raw = &tokens[*k];
            json!({"key":k,"required":i<10,"raw":if raw.is_empty(){Value::Null}else{json!(raw)},
                "resolved":if i<13{json!(raw)}else{Value::Null},"source":if i<13{"explicit"}else{"omitted"}})
        })
        .collect::<Vec<_>>();
    let raw = json!({"version":1,"kind":"theme_review","session_id":SESSION,"agent_id":AGENT,
        "backend":"custom","model":"test","snapshot":snapshot.token,"revisions":snapshot.revisions,
        "scope":"device","apply_timing":"immediate","review":"f".repeat(64),"action_id":ACTION,
        "action":"custom",
        "target":{"row_id":null,"name":name,"definition":"9".repeat(64),
            "palette":palette(),"tokens":rows},
        "overwrite":{"custom":false,"registered":true,"previous_definition":"a".repeat(64)},
        "selection_before":data()["selection"],"fields":["custom_themes","theme_name"]});
    Review::parse(&raw, snapshot, change).unwrap()
}

#[test]
fn a_picked_colour_reaches_the_existing_review_diff_and_nothing_saves_without_confirmation() {
    let mut v = picking();
    click(&mut v, FIELD.0 + 40, FIELD.1 + 2);
    let (h0, _, _) = hex_to_hsv(START).unwrap();
    let (s, val) = cell_to_sv(40, 4, 0);
    let picked = hsv_to_hex(h0, s, val);
    assert_ne!(picked, START);
    assert_eq!(accept(&mut v), picked);
    assert!(v.take_intent().is_none(), "picking dispatches nothing");
    assert!(v.review.is_none());
    assert!(v.notice.contains("nothing is saved"));
    // Back in the editor, with the picked colour in the ordinary draft field.
    let b = draw(&mut v, 80, 28);
    assert!(text(&b).contains(&format!("primary: {picked}")));
    // The existing path: Enter requests source validation of the whole draft.
    v.key(key(KeyCode::Enter));
    let Some(Intent::Validate {
        change,
        snapshot: token,
    }) = v.take_intent()
    else {
        panic!("review is requested through the existing validate intent");
    };
    assert_eq!(token, "c".repeat(64));
    let Change::Custom { tokens, .. } = &change else {
        panic!("custom change");
    };
    assert_eq!(tokens["primary"], picked);
    assert_eq!(tokens["secondary"], START, "other fields untouched");
    let review = custom_review(&change, &snapshot());
    v.observe(Update::Review(Box::new(review)));
    // The review diff shows the picked value; saving needs every page read, then Enter.
    let mut shown = text(&draw(&mut v, 80, 28));
    v.key(key(KeyCode::Enter));
    let saved_early = v.take_intent().is_some();
    for _ in 0..40 {
        let t = text(&draw(&mut v, 80, 28));
        shown.push_str(&t);
        if t.contains("Enter: save once") {
            break;
        }
        v.key(key(KeyCode::PageDown));
    }
    assert!(!saved_early, "Enter before the final page saves nothing");
    assert!(shown.contains(&format!("primary: {picked} → {picked} (explicit)")));
    assert!(shown.contains("No theme has been saved") || v.notice.contains("No theme"));
    assert!(v.take_intent().is_none(), "no save was dispatched");
    v.key(key(KeyCode::Enter));
    assert!(
        matches!(v.take_intent(), Some(Intent::Save { .. })),
        "only the explicit confirmation saves"
    );
}
