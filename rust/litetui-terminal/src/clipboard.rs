//! Explicit user-triggered clipboard boundary. Native I/O is not invoked at startup
//! or by the automated tests; tests substitute the trait boundary.
//! Copy uses Textual's OSC52 mechanism; successful output does not prove host support.
use base64::{Engine, engine::general_purpose::STANDARD};
use std::io::{Write, stdout};

pub trait Clipboard {
    fn read_text(&mut self) -> Result<Option<String>, String>;
    fn copy_text(&mut self, text: &str) -> Result<(), String>;
}

pub fn copy_sequence(text: &str) -> String {
    format!("\x1b]52;c;{}\x07", STANDARD.encode(text.as_bytes()))
}

pub struct SystemClipboard {
    max_read_bytes: usize,
}
impl SystemClipboard {
    /// Caller policy, not a production default; counts Windows allocation bytes.
    pub fn new(max_read_bytes: usize) -> Self {
        Self { max_read_bytes }
    }
}
impl Clipboard for SystemClipboard {
    #[cfg(windows)]
    fn read_text(&mut self) -> Result<Option<String>, String> {
        use clipboard_win::{Clipboard as Guard, formats, get, is_format_avail, size};
        let _guard =
            Guard::new_attempts(10).map_err(|err| format!("Cannot open clipboard: {err}"))?;
        if !is_format_avail(formats::CF_UNICODETEXT) {
            return Ok(None);
        }
        let bytes = size(formats::CF_UNICODETEXT)
            .ok_or("Clipboard text size unavailable")?
            .get();
        if bytes > self.max_read_bytes {
            return Err(format!(
                "Clipboard exceeds the {}-byte preview limit",
                self.max_read_bytes
            ));
        }
        let text: String =
            get(formats::Unicode).map_err(|err| format!("Cannot read clipboard: {err}"))?;
        Ok((!text.is_empty()).then_some(text))
    }
    #[cfg(not(windows))]
    fn read_text(&mut self) -> Result<Option<String>, String> {
        let _ = self.max_read_bytes;
        Err("OS clipboard reads are currently implemented only on Windows".into())
    }
    fn copy_text(&mut self, text: &str) -> Result<(), String> {
        let mut output = stdout().lock();
        output
            .write_all(copy_sequence(text).as_bytes())
            .and_then(|_| output.flush())
            .map_err(|err| format!("Cannot send clipboard sequence: {err}"))
    }
}
