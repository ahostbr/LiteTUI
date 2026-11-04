use litetui_terminal::protocol::{DecodeError, JsonlDecoder};
use serde_json::{Value, json};

fn decode_chunks(chunks: &[&[u8]], limit: usize) -> Result<Vec<Value>, DecodeError> {
    let mut decoder = JsonlDecoder::new(limit)?;
    let mut values = Vec::new();
    for chunk in chunks {
        decoder.push(chunk, |value| values.push(value))?;
    }
    decoder.finish()?;
    Ok(values)
}

#[test]
fn every_utf8_and_json_split_produces_the_same_object_once() {
    let bytes = "{\"type\":\"fixture_event\",\"text\":\"雪\\nquoted \\\"text\\\"\"}\n".as_bytes();
    let expected = vec![json!({"type":"fixture_event", "text":"雪\nquoted \"text\""})];
    for split in 0..=bytes.len() {
        assert_eq!(
            decode_chunks(&[&bytes[..split], &bytes[split..]], 512).unwrap(),
            expected
        );
    }
    let chunks: Vec<&[u8]> = bytes.chunks(1).collect();
    assert_eq!(decode_chunks(&chunks, 512).unwrap(), expected);
}

#[test]
fn coalesced_crlf_and_blank_lines_do_not_make_phantom_events() {
    let bytes = b"\r\n \t\n{\"type\":\"one\"}\r\n{\"type\":\"two\"}\n";
    assert_eq!(
        decode_chunks(&[bytes], 128).unwrap(),
        vec![json!({"type":"one"}), json!({"type":"two"})]
    );
}

#[test]
fn frame_limit_counts_wire_bytes_excluding_lf_and_is_not_a_product_default() {
    assert!(matches!(
        JsonlDecoder::new(0),
        Err(DecodeError::InvalidLimit)
    ));
    assert_eq!(decode_chunks(&[b"{}\n"], 2).unwrap(), vec![json!({})]);
    assert!(matches!(
        decode_chunks(&[b"{}\r\n"], 2),
        Err(DecodeError::FrameTooLarge { limit: 2 })
    ));
    assert!(matches!(
        decode_chunks(&[b"{", b"  "], 2),
        Err(DecodeError::FrameTooLarge { limit: 2 })
    ));
}

#[test]
fn malformed_frame_stops_stream_without_dropping_already_delivered_prefix() {
    let mut decoder = JsonlDecoder::new(128).unwrap();
    let mut output = Vec::new();
    let error = decoder.push(
        b"{\"type\":\"first\"}\n{broken}\n{\"type\":\"never\"}\n",
        |v| output.push(v),
    );
    assert!(matches!(error, Err(DecodeError::InvalidJson(_))));
    assert_eq!(output, vec![json!({"type":"first"})]);
    assert_eq!(
        decoder.push(b"{}\n", |_| panic!("faulted decoder emitted")),
        Err(DecodeError::Stopped)
    );
}

#[test]
fn invalid_utf8_nonobjects_and_nonstandard_numbers_are_rejected() {
    assert!(matches!(
        decode_chunks(&[b"{\"x\":\"\xff\"}\n"], 128),
        Err(DecodeError::InvalidUtf8)
    ));
    for bytes in [b"[]\n".as_slice(), b"null\n", b"1\n", b"\"text\"\n"] {
        assert_eq!(decode_chunks(&[bytes], 128), Err(DecodeError::NotObject));
    }
    assert!(matches!(
        decode_chunks(&[b"{\"x\":NaN}\n"], 128),
        Err(DecodeError::InvalidJson(_))
    ));
}

#[test]
fn opaque_numeric_payloads_are_not_rounded_to_machine_floats() {
    for number in [
        "18446744073709551617",
        "-9223372036854775809",
        "0.12345678901234567890123456789",
    ] {
        let wire = format!("{{\"type\":\"fixture_event\",\"value\":{number}}}\n");
        let output = decode_chunks(&[wire.as_bytes()], 256).unwrap();
        assert_eq!(output[0]["value"].to_string(), number);
    }
}

#[test]
fn opaque_objects_cannot_become_serde_internal_numbers() {
    let wire =
        b"{\"type\":\"fixture_event\",\"payload\":{\"$serde_json::private::Number\":\"42\"}}\n";
    let output = decode_chunks(&[wire], 256).unwrap();
    assert!(output[0]["payload"].is_object());
    assert_eq!(output[0]["payload"]["$serde_json::private::Number"], "42");
}

#[test]
fn nested_payloads_preserve_json_types_and_enforce_a_depth_bound() {
    let wire = b"{\"$serde_json::private::Number\":\"9\",\"nested\":[{\"$serde_json::private::RawValue\":\"null\"},18446744073709551617,true,null,\"text\"]}\n";
    let output = decode_chunks(&[wire], 512).unwrap();
    assert_eq!(output[0]["$serde_json::private::Number"], "9");
    assert_eq!(
        output[0]["nested"][0]["$serde_json::private::RawValue"],
        "null"
    );
    assert_eq!(output[0]["nested"][1].to_string(), "18446744073709551617");
    assert_eq!(output[0]["nested"][2], true);
    assert!(output[0]["nested"][3].is_null());
    assert_eq!(output[0]["nested"][4], "text");
    for (depth, accepted) in [(40, true), (140, false)] {
        let wire = format!(
            "{{\"nested\":{}null{}}}\n",
            "[".repeat(depth),
            "]".repeat(depth)
        );
        assert_eq!(decode_chunks(&[wire.as_bytes()], 1024).is_ok(), accepted);
    }
}

#[test]
fn repeated_authority_fields_fail_before_the_frame_reaches_a_consumer() {
    for wire in [
        "{\"id\":\"one\",\"id\":\"two\"}\n",
        "{\"review\":{\"tools_enabled\":false,\"tools_enabled\":true}}\n",
    ] {
        assert!(matches!(
            decode_chunks(&[wire.as_bytes()], 256),
            Err(DecodeError::InvalidJson(_))
        ));
    }
}

#[test]
fn duplicate_keys_cannot_hide_an_overdepth_wire_subtree() {
    let wire = format!(
        "{{\"x\":{}null{},\"x\":null}}\n",
        "[".repeat(200),
        "]".repeat(200)
    );
    let mut decoder = JsonlDecoder::new(1024).unwrap();
    let mut output = Vec::new();
    assert!(matches!(
        decoder.push(wire.as_bytes(), |value| output.push(value)),
        Err(DecodeError::InvalidJson(_))
    ));
    assert!(output.is_empty());
    assert_eq!(
        decoder.push(b"{}\n", |_| panic!("faulted decoder emitted")),
        Err(DecodeError::Stopped)
    );
}

#[test]
fn eof_requires_complete_frames_and_cannot_reuse_a_connection_decoder() {
    let mut decoder = JsonlDecoder::new(128).unwrap();
    decoder
        .push(b"{\"type\":\"partial\"}", |_| {
            panic!("unterminated frame emitted")
        })
        .unwrap();
    assert_eq!(decoder.finish(), Err(DecodeError::TruncatedFrame));
    assert_eq!(decoder.push(b"\n", |_| {}), Err(DecodeError::Stopped));
    let mut clean = JsonlDecoder::new(128).unwrap();
    assert_eq!(clean.finish(), Ok(()));
    assert_eq!(clean.push(b"{}\n", |_| {}), Err(DecodeError::Stopped));
}
