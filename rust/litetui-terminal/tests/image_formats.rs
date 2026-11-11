use litetui_terminal::{
    image::{self, Picker, Request},
    theme::builtin_themes,
    work_protocol::Identity,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

fn specimen(format: &str) -> (Identity, Value, String) {
    let id = Identity {
        session: "session".into(),
        agent: "agent".into(),
        backend: "custom".into(),
        model: "vision".into(),
    };
    let path = format!("C:/private/chosen.{}", format.to_lowercase());
    let mut wire = id.request();
    wire.as_object_mut().unwrap().extend(json!({"kind":"image_review", "work_instance":"work", "token":"a".repeat(64), "delivery":"inline", "name":format!("chosen.{}",format.to_lowercase()), "format":format, "width":12, "height":8, "sent_width":12, "sent_height":8, "png_bytes":100, "png_sha256":"b".repeat(64)}).as_object().unwrap().clone());
    (id, wire, path)
}

#[test]
fn file_and_replacement_accept_source_formats_without_weakening_identity_or_bounds() {
    for format in ["PNG", "JPEG", "WEBP", "GIF", "BMP"] {
        let (id, wire, path) = specimen(format);
        for request in [
            Request::File(path.clone()),
            Request::ReplaceFile {
                path,
                previous: "c".repeat(64),
            },
        ] {
            assert!(
                image::parse(&wire, &id, "work", &request)
                    .unwrap()
                    .is_some()
            );
            for (key, invalid) in [
                ("name", json!("other.gif")),
                ("work_instance", json!("other")),
                ("width", json!(16_000_001)),
                ("format", json!("SVG")),
            ] {
                let mut altered = wire.clone();
                altered[key] = invalid;
                assert!(
                    image::parse(&altered, &id, "work", &request).is_err(),
                    "{format}: {key}"
                );
            }
        }
    }
}

#[test]
fn image_review_discloses_first_frame_before_an_attachment_is_committed() {
    let (id, wire, path) = specimen("GIF");
    let request = Request::File(path);
    let mut picker = Picker::default();
    picker.open();
    picker.result(
        &request,
        image::parse(&wire, &id, "work", &request).unwrap(),
    );
    let mut terminal = Terminal::new(TestBackend::new(140, 40)).unwrap();
    let theme = builtin_themes().remove(0);
    terminal
        .draw(|frame| picker.draw(frame, frame.area(), &theme, false))
        .unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    assert!(text.contains("Only the first image frame is sent."));
    assert!(picker.attached.is_none());
}
