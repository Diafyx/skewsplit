//! The crate against the behaviour spec in the repository.

use std::path::Path;

#[test]
fn spec_version_is_the_version_in_spec_md() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../spec/SPEC.md");
    let text = std::fs::read_to_string(&path).expect("spec/SPEC.md is readable");
    let lines: Vec<&str> = text
        .lines()
        .filter(|line| line.starts_with("Spec version: "))
        .collect();
    assert_eq!(
        lines,
        [format!("Spec version: **{}**", skewsplit::SPEC_VERSION)]
    );
}
