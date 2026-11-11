//! Section-specific plans; ownership, publication and editing stay in the parent.
use super::*;

const DEFAULTS: [&str; 9] = [
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "stop",
];
const MAPS: [&str; 3] = [
    "model_infer_overrides",
    "llama_load_settings",
    "llama_presets",
];
pub(super) const REQUEST: [&str; 13] = [
    "temperature",
    "top_p",
    "max_tokens",
    "top_k",
    "min_p",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "reasoning_effort",
    "enable_thinking",
    "json_schema",
    "seed",
];

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Section {
    #[default]
    Model,
    Defaults,
    Presets,
}
impl Section {
    pub(super) fn wire(self) -> &'static str {
        match self {
            Self::Model => "model",
            Self::Defaults => "defaults",
            Self::Presets => "presets",
        }
    }
    pub(super) fn parse(v: &Value) -> io::Result<Self> {
        match v.get("section") {
            None => Ok(Self::Model),
            Some(Value::String(s)) if s == "defaults" => Ok(Self::Defaults),
            Some(Value::String(s)) if s == "presets" => Ok(Self::Presets),
            _ => Err(bad()),
        }
    }
    pub(super) fn fields(self) -> &'static [&'static str] {
        if self == Self::Defaults {
            &DEFAULTS
        } else {
            &FIELDS
        }
    }
    pub(super) fn list(self) -> Request {
        if self == Self::Model {
            Request::List
        } else {
            Request::Section(self)
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum PresetAction {
    Save,
    Apply,
}
impl PresetAction {
    pub(super) fn wire(self) -> &'static str {
        if self == Self::Save { "save" } else { "apply" }
    }
}
fn object(v: &Value) -> io::Result<&serde_json::Map<String, Value>> {
    v.as_object().ok_or_else(bad)
}
fn preset(v: &Value) -> io::Result<()> {
    let values = object(v)?;
    if values.keys().any(|k| k != "inference" && k != "load") || v.to_string().len() > 128 * 1024 {
        return Err(bad());
    }
    for row in values.values() {
        object(row)?;
    }
    Ok(())
}
fn name(s: &str) -> bool {
    !s.is_empty() && s.trim() == s && s.len() <= 256
}

pub(super) fn parse_details(
    v: &Value,
    section: Section,
    fields: &BTreeMap<String, Field>,
) -> io::Result<Value> {
    if section == Section::Model {
        return Ok(Value::Null);
    }
    keys(&v["destinations"], &["conversation", "defaults"])?;
    for key in ["conversation", "defaults"] {
        if !v["destinations"][key]
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.len() <= 32768)
        {
            return Err(bad());
        }
    }
    keys(&v["settings"], &["saved", "active"])?;
    keys(&v["request"], &REQUEST)?;
    if REQUEST.iter().any(|k| !wire_ok(k, &v["request"][k])) {
        return Err(bad());
    }
    for state in ["saved", "active"] {
        keys(
            &v["settings"][state],
            if section == Section::Defaults {
                &DEFAULTS
            } else {
                &MAPS
            },
        )?;
        if section == Section::Defaults {
            if v["raw"]["inference"][state] != v["settings"][state]
                || v["raw"]["load"][state] != json!({})
            {
                return Err(bad());
            }
        } else {
            for key in MAPS {
                object(&v["settings"][state][key])?;
            }
            for (group, setting) in [
                ("inference", "model_infer_overrides"),
                ("load", "llama_load_settings"),
            ] {
                let rows = object(&v["settings"][state][setting])?;
                let row = rows
                    .get(v["model"].as_str().ok_or_else(bad)?)
                    .cloned()
                    .unwrap_or(json!({}));
                if row != v["raw"][group][state] {
                    return Err(bad());
                }
            }
        }
    }
    for (key, row) in fields {
        if (section == Section::Defaults || !load_field(key)) && row.next != v["request"][key] {
            return Err(bad());
        }
    }
    let extra_key = if section == Section::Defaults {
        "model_overrides"
    } else {
        "presets"
    };
    let extra = object(&v[extra_key])?;
    if section == Section::Defaults {
        for key in DEFAULTS {
            if fields[key].overridden != extra.contains_key(key) {
                return Err(bad());
            }
        }
    } else {
        if v["presets"] != v["settings"]["saved"]["llama_presets"] {
            return Err(bad());
        }
        for (k, value) in extra {
            if !name(k) {
                return Err(bad());
            }
            preset(value)?;
        }
    }
    Ok(json!({
        "destinations":v["destinations"], "settings":v["settings"], "request":v["request"],
        extra_key:v[extra_key],
    }))
}

pub(super) fn plan(edit: &Edit) -> io::Result<Value> {
    let s = &edit.before;
    if !s.supported || s.section == Section::Model {
        return Err(bad());
    }
    let mut changes = json!({});
    let mut affected = Vec::new();
    let mut next = s.details["request"].clone();
    let scope;
    if s.section == Section::Defaults {
        let row = s
            .fields
            .get(&edit.field)
            .filter(|r| r.editable)
            .ok_or_else(bad)?;
        if edit.inherit
            || edit.preset.is_some()
            || !s.catalog[&edit.field].accepts(&edit.field, &edit.value)
        {
            return Err(bad());
        }
        let actual = if edit.field == "stop" && edit.value.is_null() {
            json!([])
        } else {
            edit.value.clone()
        };
        if actual == row.saved {
            return Err(bad());
        }
        changes[&edit.field] = actual.clone();
        affected.push(
            json!({"group":"default", "field":edit.field, "before":row.saved, "after":actual}),
        );
        if !row.overridden {
            next[&edit.field] = if actual == json!([]) {
                Value::Null
            } else {
                actual
            };
        }
        scope = "conversation";
    } else {
        if !name(&edit.field) || !edit.value.is_null() || edit.inherit {
            return Err(bad());
        }
        match edit.preset.ok_or_else(bad)? {
            PresetAction::Save => {
                if s.details["settings"]["saved"]["llama_presets"]
                    != s.details["settings"]["active"]["llama_presets"]
                {
                    return Err(bad());
                }
                let desired =
                    json!({"inference":s.raw["inference"]["saved"], "load":s.raw["load"]["saved"]});
                preset(&desired)?;
                let mut presets = s.details["settings"]["saved"]["llama_presets"].clone();
                if presets.get(&edit.field) == Some(&desired) {
                    return Err(bad());
                }
                affected.push(json!({"group":"preset", "field":edit.field, "before":presets[&edit.field], "after":desired}));
                presets[&edit.field] = desired;
                changes["llama_presets"] = presets;
                scope = "defaults";
            }
            PresetAction::Apply => {
                let desired = s.details["presets"].get(&edit.field).ok_or_else(bad)?;
                preset(desired)?;
                for (group, setting) in [
                    ("inference", "model_infer_overrides"),
                    ("load", "llama_load_settings"),
                ] {
                    let mut rows = s.details["settings"]["saved"][setting].clone();
                    let old = rows.get(&s.model).cloned().unwrap_or(json!({}));
                    let mut row = old.clone();
                    if let Some(patch) = desired.get(group) {
                        for (k, value) in object(patch)? {
                            row[k] = value.clone();
                        }
                    }
                    for (key, value) in object(&row)? {
                        if old.get(key) == Some(value) {
                            continue;
                        }
                        if let Some(meta) = s.catalog.get(key).filter(|m| m.group == group)
                            && (!s.fields[key].editable || !meta.accepts(key, value))
                        {
                            return Err(bad());
                        }
                        if group == "inference" && key == "seed" && !value_ok(key, value) {
                            return Err(bad());
                        }
                        affected.push(
                            json!({"group":group,"field":key,"before":old[key],"after":value}),
                        );
                        if group == "inference"
                            && let Some(meta) = s.catalog.get(key).filter(|m| m.group == group)
                        {
                            next[key] = if value.is_null() {
                                s.fields[key].server_default.clone()
                            } else {
                                meta.wire(key, value)?
                            };
                        }
                        if group == "inference" && key == "seed" {
                            next[key] = value.clone();
                        }
                    }
                    if !object(&row)?.is_empty() {
                        rows[&s.model] = row;
                    }
                    changes[setting] = rows;
                }
                if affected.is_empty() {
                    return Err(bad());
                }
                scope = "conversation";
            }
        }
    }
    affected.sort_by(|a, b| {
        (a["group"].as_str(), a["field"].as_str()).cmp(&(b["group"].as_str(), b["field"].as_str()))
    });
    let effective = object(&changes)?
        .keys()
        .map(|key| (key.clone(), s.details["settings"]["active"][key].clone()))
        .collect::<serde_json::Map<_, _>>();
    let pending_load = changes
        .get("llama_load_settings")
        .is_some_and(|v| v != &s.details["settings"]["active"]["llama_load_settings"]);
    Ok(
        json!({"scope":scope, "destination":s.details["destinations"][scope], "changes":changes,
        "effective_before":effective, "affected":affected,"next_request":next,"pending_load":pending_load}),
    )
}

pub(super) fn after(edit: &Edit, transaction: &Value, revision: &str) -> io::Result<Snapshot> {
    if transaction != &plan(edit)? {
        return Err(bad());
    }
    let mut expected = edit.before.clone();
    expected.revision = revision.into();
    for (key, value) in object(&transaction["changes"])? {
        expected.details["settings"]["saved"][key] = value.clone();
        if key != "llama_load_settings" {
            expected.details["settings"]["active"][key] = value.clone();
        }
    }
    expected.details["request"] = transaction["next_request"].clone();
    if expected.section == Section::Defaults {
        expected.raw["inference"] = expected.details["settings"].clone();
    } else {
        expected.details["presets"] =
            expected.details["settings"]["saved"]["llama_presets"].clone();
        for (group, setting) in [
            ("inference", "model_infer_overrides"),
            ("load", "llama_load_settings"),
        ] {
            for state in ["saved", "active"] {
                expected.raw[group][state] = expected.details["settings"][state][setting]
                    .get(&expected.model)
                    .cloned()
                    .unwrap_or(json!({}));
            }
        }
    }
    for (key, row) in &mut expected.fields {
        let group = &expected.catalog[key].group;
        row.present = expected.raw[group]["saved"].get(key).is_some();
        row.active_present = expected.raw[group]["active"].get(key).is_some();
        row.saved = expected.raw[group]["saved"][key].clone();
        row.active = expected.raw[group]["active"][key].clone();
        if expected.section == Section::Defaults || !load_field(key) {
            row.next = transaction["next_request"][key].clone();
        }
    }
    Ok(expected)
}

pub(super) fn review_lines(review: &Review) -> Vec<String> {
    let tx = &review.transaction;
    let mut lines = vec![
        format!("Review {} {}", mode(&review.edit), review.edit.field),
        format!(
            "Destination ({}): {}",
            tx["scope"].as_str().unwrap_or(""),
            tx["destination"].as_str().unwrap_or("")
        ),
    ];
    if let Some(items) = tx["affected"].as_array() {
        for item in items {
            lines.push(format!(
                "{} / {}: {} → {}",
                item["group"].as_str().unwrap_or(""),
                item["field"].as_str().unwrap_or(""),
                item["before"],
                item["after"]
            ));
        }
    }
    if review.edit.before.section == Section::Defaults {
        let key = &review.edit.field;
        lines.push(format!(
            "Next request {}: {}{}",
            key,
            show(&tx["next_request"][key]),
            if review.edit.before.fields[key].overridden {
                " (selected-model override masks this default)"
            } else {
                ""
            }
        ));
    } else if review.edit.preset == Some(PresetAction::Save) {
        lines.push("Save the current SAVED model rows, including pending load settings. Existing named preset is overwritten only after this review.".into());
    } else {
        lines.push(format!("Next request inference: {}", tx["next_request"]));
    }
    lines.push(
        if tx["pending_load"] == true {
            "Load changes remain pending. Reload/reopen explicitly; no model load occurs."
        } else {
            "No model load occurs."
        }
        .into(),
    );
    lines.push("Enter save/apply · Esc cancel · PgUp/PgDn read all affected fields".into());
    lines
}
