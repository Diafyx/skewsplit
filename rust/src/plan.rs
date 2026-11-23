//! A plan: the predicates, their estimated rows and the warnings (spec
//! section 10), as JSON and as a report for people.

use crate::SPEC_VERSION;
use crate::error::SpecError;
use crate::json::{JsonError, Object, Value};

/// The comparison at one end of a range.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    /// `>=`: from a split point.
    AtLeast,
    /// `>`: just above a dominant value.
    Above,
    /// `<`: up to a split point or a dominant value.
    Below,
}

impl Op {
    /// The operator as SQL writes it.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AtLeast => ">=",
            Self::Above => ">",
            Self::Below => "<",
        }
    }
}

/// One end of a range: the operator and the value in its text form.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Bound {
    /// `>=` or `>` below, `<` above.
    pub op: Op,
    /// The value's text form (spec section 2).
    pub value: String,
}

impl Bound {
    fn to_value(&self) -> Value {
        let mut out = Object::new();
        out.insert("op", Value::from(self.op.as_str()));
        out.insert("value", Value::from(self.value.as_str()));
        Value::Object(out)
    }
}

/// What a predicate matches.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Kind {
    /// The rows where the column is NULL.
    Null,
    /// The rows of one dominant value, in its text form.
    Value(String),
    /// The values between two ends; `None` is an open end.
    Range {
        /// The lower end, or `None` for no lower end.
        lower: Option<Bound>,
        /// The upper end, or `None` for no upper end.
        upper: Option<Bound>,
    },
    /// The rows whose hash leaves this remainder (spec section 16).
    Hash(i128),
}

/// One predicate of a plan, with the rows it is estimated to read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Predicate {
    /// What the predicate matches.
    pub kind: Kind,
    /// The predicate as SQL, for Spark's JDBC reader.
    pub sql: String,
    /// The rows the estimate model puts in it.
    pub estimated_rows: i128,
}

impl Predicate {
    /// The predicate as spec section 10 writes it.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut out = Object::new();
        let kind = match &self.kind {
            Kind::Null => "null",
            Kind::Value(_) => "value",
            Kind::Range { .. } => "range",
            Kind::Hash(_) => "hash",
        };
        out.insert("kind", Value::from(kind));
        match &self.kind {
            Kind::Null => {}
            Kind::Value(value) => out.insert("value", Value::from(value.as_str())),
            Kind::Range { lower, upper } => {
                out.insert("lower", lower.as_ref().map_or(Value::Null, Bound::to_value));
                out.insert("upper", upper.as_ref().map_or(Value::Null, Bound::to_value));
            }
            Kind::Hash(remainder) => out.insert("remainder", Value::from(*remainder)),
        }
        out.insert("sql", Value::from(self.sql.as_str()));
        out.insert("estimated_rows", Value::from(self.estimated_rows));
        Value::Object(out)
    }
}

/// A warning code of spec section 8.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WarningCode {
    /// Hash predicates read the whole table each (spec section 16).
    FullScans,
    /// A value holds at least a partition's share and has its own predicate.
    DominantValue,
    /// A value holds at least a partition's share, but has no predicate of its own.
    DominantValueNotIsolated,
    /// The plan has fewer predicates than requested.
    FewerPartitions,
    /// The NULL rows are more than a partition's share.
    NullsExceedShare,
    /// The input has no rows.
    NoRows,
}

impl WarningCode {
    /// The code as the spec writes it, such as `dominant_value`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FullScans => "full_scans",
            Self::DominantValue => "dominant_value",
            Self::DominantValueNotIsolated => "dominant_value_not_isolated",
            Self::FewerPartitions => "fewer_partitions",
            Self::NullsExceedShare => "nulls_exceed_share",
            Self::NoRows => "no_rows",
        }
    }
}

/// A warning of spec section 8. Fields that a code does not use are `None`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlanWarning {
    /// The code.
    pub code: WarningCode,
    /// A description for people, whose wording is not part of the contract.
    pub message: String,
    /// The value it is about, in its text form.
    pub value: Option<String>,
    /// The rows it is about.
    pub rows: Option<i128>,
    /// The partitions requested.
    pub requested: Option<i128>,
    /// The partitions planned.
    pub planned: Option<i128>,
}

impl PlanWarning {
    /// A warning with only a code and a message.
    #[must_use]
    pub fn new(code: WarningCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            value: None,
            rows: None,
            requested: None,
            planned: None,
        }
    }

    /// The warning with the fields its code defines, then the message.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut out = Object::new();
        out.insert("code", Value::from(self.code.as_str()));
        if let Some(value) = &self.value {
            out.insert("value", Value::from(value.as_str()));
        }
        for (key, field) in [
            ("rows", self.rows),
            ("requested", self.requested),
            ("planned", self.planned),
        ] {
            if let Some(field) = field {
                out.insert(key, Value::from(field));
            }
        }
        out.insert("message", Value::from(self.message.as_str()));
        Value::Object(out)
    }
}

/// The planner's output for one column.
#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    /// The column name, unquoted.
    pub column: String,
    /// The column type.
    pub type_name: String,
    /// The `numeric` scale, or `None` for other types.
    pub scale: Option<u32>,
    /// `N`.
    pub requested_partitions: i128,
    /// Rows where the column is NULL.
    pub null_rows: i128,
    /// All rows.
    pub total_rows: i128,
    /// Each predicate with its estimated rows: the NULL predicate first, then
    /// the others in value order. These are the `predicates` of the JSON.
    pub parts: Vec<Predicate>,
    /// The skew score times 1000 (spec section 7), or `None` without rows.
    pub skew_milli: Option<i128>,
    /// In the order of spec section 8.
    pub warnings: Vec<PlanWarning>,
    /// Where the estimate came from, as the input's `source` said, or `null`.
    pub source: Value,
}

impl Plan {
    /// The SQL of each predicate, in order: the `predicates` of Spark's JDBC reader.
    #[must_use]
    pub fn predicates(&self) -> Vec<&str> {
        self.parts.iter().map(|part| part.sql.as_str()).collect()
    }

    /// The number of predicates besides the NULL predicate.
    #[must_use]
    pub fn planned_partitions(&self) -> i128 {
        // A plan has at most 10001 predicates.
        i128::try_from(self.parts.len()).unwrap_or(i128::MAX) - 1
    }

    /// The skew score as the JSON writes it, `k / 1000`, or `None` without rows.
    #[must_use]
    pub fn skew_score(&self) -> Option<f64> {
        // k is at most N * 1000 + 1, so it is exact as an f64, and k / 1000 is
        // the float nearest the score, as Python's division gives it.
        let k = i32::try_from(self.skew_milli?).ok()?;
        Some(f64::from(k) / 1000.0)
    }

    /// The plan as the JSON object of spec section 10.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut out = Object::new();
        out.insert("spec_version", Value::from(SPEC_VERSION));
        out.insert("column", Value::from(self.column.as_str()));
        out.insert("type", Value::from(self.type_name.as_str()));
        if let Some(scale) = self.scale {
            out.insert("scale", Value::from(i128::from(scale)));
        }
        out.insert(
            "requested_partitions",
            Value::from(self.requested_partitions),
        );
        out.insert("planned_partitions", Value::from(self.planned_partitions()));
        let mut rows = Object::new();
        rows.insert("total", Value::from(self.total_rows));
        rows.insert("null", Value::from(self.null_rows));
        rows.insert("non_null", Value::from(self.total_rows - self.null_rows));
        out.insert("rows", Value::Object(rows));
        let parts = self.parts.iter().map(Predicate::to_value).collect();
        out.insert("predicates", Value::Array(parts));
        out.insert(
            "skew_score",
            self.skew_score().map_or(Value::Null, Value::Float),
        );
        let warnings = self.warnings.iter().map(PlanWarning::to_value).collect();
        out.insert("warnings", Value::Array(warnings));
        out.insert("source", self.source.clone());
        Value::Object(out)
    }

    /// The plan as JSON text, as the Python core writes it: on one line, or
    /// with `indent` spaces per level.
    ///
    /// # Errors
    ///
    /// If `source` holds `NaN` or an infinity, which JSON has no form for.
    pub fn to_json(&self, indent: Option<usize>) -> Result<String, JsonError> {
        self.to_value().to_json(indent)
    }

    /// The skew report, as text for people: a summary line, a line on the
    /// strategy `auto` chose (spec section 17), a line on the sample for a
    /// plan from one (section 14), each predicate with its estimated rows,
    /// and a line for each warning, the plan's and then those in
    /// `source["warnings"]`. The layout may change; read the JSON in code.
    #[must_use]
    pub fn report(&self) -> String {
        let kind = match self.scale {
            Some(scale) => format!("{}, scale {scale}", self.type_name),
            None => self.type_name.clone(),
        };
        let skew = match self.skew_milli {
            Some(k) => format!("skew score {}", milli_text(k)),
            None => "no skew score".to_owned(),
        };
        let mut lines = vec![format!(
            "{} ({kind}): {} estimated rows, {} of {} partitions plus one for NULL, {skew}",
            self.column,
            self.total_rows,
            self.planned_partitions(),
            self.requested_partitions
        )];
        lines.extend(auto_line(&self.source));
        lines.extend(sample_line(&self.source));
        let width = self
            .parts
            .iter()
            .map(|part| part.estimated_rows.to_string().len())
            .fold("rows".len(), usize::max);
        lines.push(format!("{:>width$}  predicate", "rows"));
        for part in &self.parts {
            lines.push(format!("{:>width$}  {}", part.estimated_rows, part.sql));
        }
        let warnings = self
            .warnings
            .iter()
            .map(|warning| (warning.code.as_str(), warning.message.as_str()))
            .chain(source_warnings(&self.source));
        lines.extend(warnings.map(|(code, message)| format!("warning {code}: {message}")));
        lines.join("\n")
    }
}

/// An error as the JSON object of spec section 10.
#[must_use]
pub fn error_value(error: &SpecError) -> Value {
    let mut detail = Object::new();
    detail.insert("code", Value::from(error.code().as_str()));
    detail.insert("message", Value::from(error.message()));
    let mut out = Object::new();
    out.insert("spec_version", Value::from(SPEC_VERSION));
    out.insert("error", Value::Object(detail));
    Value::Object(out)
}

/// `k / 1000` with three decimals, from the integer, as Python's
/// `f"{k / 1000:.3f}"` writes it for every k a plan has.
fn milli_text(k: i128) -> String {
    format!("{}.{:03}", k / 1000, k % 1000)
}

/// The strategy that `auto` chose and why, if it chose one.
fn auto_line(source: &Value) -> Option<String> {
    let choice = source.get("auto")?;
    let strategy = choice.get("strategy")?.as_str()?;
    let message = choice.get("message")?.as_str()?;
    Some(format!("strategy {strategy}, chosen by auto: {message}"))
}

/// The sample a plan came from, if it came from one.
fn sample_line(source: &Value) -> Option<String> {
    let rows = source.get("sample_rows")?.as_int()?;
    let mut line = format!("from a sample of {rows} rows");
    // Python formats an integer with `.3f` by converting it to a float first.
    let elapsed = match source.get("planning_ms") {
        Some(Value::Int(ms)) => ms.to_string().parse().ok().map(fixed_3),
        Some(Value::Float(ms)) => Some(fixed_3(*ms)),
        _ => None,
    };
    if let Some(elapsed) = elapsed {
        line = format!("{line}, planned in {elapsed} ms");
    }
    Some(line)
}

/// A float with three decimals, as Python's `f"{x:.3f}"` writes it: rounded
/// half to even from the float's exact value, with `nan`, `inf` and `-inf`.
fn fixed_3(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    format!("{x:.3}")
}

/// The warnings an adapter added to the source, each with a code and a message.
fn source_warnings(source: &Value) -> impl Iterator<Item = (&str, &str)> {
    source
        .get("warnings")
        .and_then(Value::as_array)
        .unwrap_or_default()
        .iter()
        .filter_map(|warning| {
            let code = warning.get("code")?.as_str()?;
            let message = warning.get("message")?.as_str()?;
            Some((code, message))
        })
}
