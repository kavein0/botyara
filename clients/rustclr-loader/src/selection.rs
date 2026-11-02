pub const PROFILES: [&str; 3] = ["standard", "mixed", "host"];

// None means explicit config: the managed client applies its saved rules or standard.
pub fn select<'a>(profile: Option<&'a str>, config: Option<&str>, draw: impl FnOnce() -> usize) -> (Option<&'a str>, &'static str) {
    if let Some(profile) = profile { return (Some(profile), "explicit BOT_PROFILE"); }
    if config.is_some() { return (None, "explicit BOT_CONFIG (saved rules, or standard for a new file)"); }
    (Some(PROFILES[draw()]), "random")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_choices_and_priorities() {
        for (index, expected) in PROFILES.iter().enumerate() {
            assert_eq!(select(None, None, || index), (Some(*expected), "random"));
        }
        assert_eq!(select(Some("host"), Some("custom.json"), || panic!("must not draw")).0, Some("host"));
        assert_eq!(select(None, Some("custom.json"), || panic!("must not draw")).0, None);
        assert_eq!(select(Some("mixed"), None, || panic!("must not draw")).0, Some("mixed"));
    }
}
