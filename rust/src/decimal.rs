//! Exact arithmetic on non-negative decimal numbers of any size, for the
//! conversion of `pg_stats` (spec section 13).
//!
//! The spec asks for exact arithmetic on texts such as `1.5e+07`, with up to
//! 40 characters and exponents up to 999, so the numbers can be far beyond
//! `i128`. Rust's standard library has no numbers of any size and the crate
//! has no dependencies, so this module has the few operations the conversion
//! needs. Every number there is a whole number times a power of 10, so
//! rounding only drops decimal digits and needs no general division.

use std::cmp::Ordering;
use std::fmt;

use crate::json::{Int, Value};

/// A limb holds 9 decimal digits, so printing and shifting by powers of 10
/// stay simple, and the product of two limbs fits in a `u64`.
const BASE: u64 = 1_000_000_000;
const LIMB_DIGITS: usize = 9;

/// A natural number of any size.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct Natural {
    // Base 10^9, least significant limb first, with no zero limb at the end,
    // so 0 has no limbs and every number has one form.
    limbs: Vec<u64>,
}

impl Natural {
    /// The number with these ASCII decimal digits, leading zeros allowed.
    pub(crate) fn from_digits(digits: &str) -> Self {
        let limbs = digits
            .as_bytes()
            .rchunks(LIMB_DIGITS)
            .map(|chunk| {
                chunk
                    .iter()
                    .fold(0, |limb, &digit| limb * 10 + u64::from(digit - b'0'))
            })
            .collect();
        Self { limbs }.trimmed()
    }

    /// The number of a JSON integer that is not negative.
    pub(crate) fn from_int(int: &Int) -> Self {
        Self::from_digits(&int.to_string())
    }

    /// `10^exponent`.
    pub(crate) fn pow10(exponent: usize) -> Self {
        Self::from(1).mul_pow10(exponent)
    }

    /// The number as a JSON integer.
    pub(crate) fn to_value(&self) -> Value {
        Value::Int(Int::from_digits(self.to_string()))
    }

    pub(crate) fn add(&self, other: &Self) -> Self {
        let (long, short) = if self.limbs.len() >= other.limbs.len() {
            (self, other)
        } else {
            (other, self)
        };
        let mut limbs = Vec::with_capacity(long.limbs.len() + 1);
        let mut carry = 0;
        for (i, &limb) in long.limbs.iter().enumerate() {
            let sum = limb + short.limbs.get(i).copied().unwrap_or_default() + carry;
            limbs.push(sum % BASE);
            carry = sum / BASE;
        }
        limbs.push(carry);
        Self { limbs }.trimmed()
    }

    /// `self - other`, or 0 if `other` is larger.
    pub(crate) fn saturating_sub(&self, other: &Self) -> Self {
        if self <= other {
            return Self::default();
        }
        let mut limbs = Vec::with_capacity(self.limbs.len());
        let mut borrow = 0;
        for (i, &limb) in self.limbs.iter().enumerate() {
            let take = other.limbs.get(i).copied().unwrap_or_default() + borrow;
            borrow = u64::from(limb < take);
            limbs.push(limb + borrow * BASE - take);
        }
        Self { limbs }.trimmed()
    }

    pub(crate) fn mul(&self, other: &Self) -> Self {
        let mut limbs = vec![0; self.limbs.len() + other.limbs.len()];
        for (i, &a) in self.limbs.iter().enumerate() {
            let mut carry = 0;
            for (j, &b) in other.limbs.iter().enumerate() {
                // At most (BASE - 1) + (BASE - 1)^2 + (BASE - 1) = BASE^2 - 1,
                // which fits in a u64, so the carry stays below BASE.
                let product = limbs[i + j] + a * b + carry;
                limbs[i + j] = product % BASE;
                carry = product / BASE;
            }
            limbs[i + other.limbs.len()] = carry;
        }
        Self { limbs }.trimmed()
    }

    pub(crate) fn mul_small(&self, factor: u64) -> Self {
        let mut limbs = Vec::with_capacity(self.limbs.len() + 3);
        // A limb times a u64 is below 2^94, so a u128 holds it with the carry.
        let mut carry: u128 = 0;
        for &limb in &self.limbs {
            let product = u128::from(limb) * u128::from(factor) + carry;
            limbs.push(low(product));
            carry = product / u128::from(BASE);
        }
        // The carry is below the factor, so it fills at most 3 limbs, as
        // 10^27 is above 2^64. A fixed count, not `while carry > 0`, keeps
        // every mutant of this loop from pushing limbs until memory runs out.
        for _ in 0..3 {
            limbs.push(low(carry));
            carry /= u128::from(BASE);
        }
        Self { limbs }.trimmed()
    }

    /// `floor(self / divisor)`, for a divisor above 0.
    pub(crate) fn div_small(&self, divisor: u64) -> Self {
        let mut limbs = self.limbs.clone();
        // The remainder is below the divisor, so each quotient limb is below
        // BASE and the dividend below 2^94.
        let mut remainder: u128 = 0;
        for limb in limbs.iter_mut().rev() {
            let dividend = remainder * u128::from(BASE) + u128::from(*limb);
            *limb = low(dividend / u128::from(divisor));
            remainder = dividend % u128::from(divisor);
        }
        Self { limbs }.trimmed()
    }

    /// `self × 10^exponent`.
    pub(crate) fn mul_pow10(&self, exponent: usize) -> Self {
        if self.limbs.is_empty() {
            return Self::default();
        }
        let mut limbs = vec![0; exponent / LIMB_DIGITS];
        limbs.extend(&self.limbs);
        Self { limbs }.mul_small(small_pow10(exponent % LIMB_DIGITS))
    }

    /// `floor(self / 10^exponent)`.
    pub(crate) fn div_pow10(&self, exponent: usize) -> Self {
        let limbs = self
            .limbs
            .get(exponent / LIMB_DIGITS..)
            .unwrap_or_default()
            .to_vec();
        Self { limbs }.div_small(small_pow10(exponent % LIMB_DIGITS))
    }

    fn trimmed(mut self) -> Self {
        while self.limbs.last() == Some(&0) {
            self.limbs.pop();
        }
        self
    }
}

impl From<u64> for Natural {
    fn from(value: u64) -> Self {
        Self {
            limbs: vec![value % BASE, value / BASE % BASE, value / BASE / BASE],
        }
        .trimmed()
    }
}

impl Ord for Natural {
    fn cmp(&self, other: &Self) -> Ordering {
        // Without zero limbs at the end, more limbs is a larger number.
        self.limbs
            .len()
            .cmp(&other.limbs.len())
            .then_with(|| self.limbs.iter().rev().cmp(other.limbs.iter().rev()))
    }
}

impl PartialOrd for Natural {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl fmt::Display for Natural {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut limbs = self.limbs.iter().rev();
        let first = limbs.next().copied().unwrap_or_default();
        limbs.fold(write!(f, "{first}"), |written, limb| {
            written.and_then(|()| write!(f, "{limb:09}"))
        })
    }
}

/// `digits × 10^exponent`: a non-negative decimal number, exactly.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct Decimal {
    digits: Natural,
    exponent: i64,
}

impl Decimal {
    /// Read a text of the form `(0|[1-9][0-9]*)(\.[0-9]+)?(e[+-][0-9]{2,3})?`,
    /// or `None` for any other text.
    pub(crate) fn parse(text: &str) -> Option<Self> {
        let is_digits = |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit());
        let (mantissa, exponent) = match text.split_once('e') {
            Some((mantissa, exponent)) => (mantissa, Some(exponent)),
            None => (text, None),
        };
        let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, "0"));
        if !is_digits(whole) || (whole.len() > 1 && whole.starts_with('0')) || !is_digits(fraction)
        {
            return None;
        }
        let shift = match exponent {
            None => 0,
            Some(exponent) => {
                let (sign, digits) = match exponent.strip_prefix('-') {
                    Some(digits) => (-1, digits),
                    None => (1, exponent.strip_prefix('+')?),
                };
                if !is_digits(digits) || !(2..=3).contains(&digits.len()) {
                    return None;
                }
                sign * digits
                    .bytes()
                    .fold(0, |value, digit| value * 10 + i64::from(digit - b'0'))
            }
        };
        // The text is short, so its fraction's length fits in an i64.
        let places = i64::try_from(fraction.len()).unwrap_or(i64::MAX);
        Some(Self {
            digits: Natural::from_digits(&format!("{whole}{fraction}")),
            exponent: shift - places,
        })
    }

    pub(crate) fn add(&self, other: &Self) -> Self {
        let exponent = self.exponent.min(other.exponent);
        Self {
            digits: self.shifted(exponent).add(&other.shifted(exponent)),
            exponent,
        }
    }

    pub(crate) fn mul(&self, other: &Self) -> Self {
        Self {
            digits: self.digits.mul(&other.digits),
            exponent: self.exponent + other.exponent,
        }
    }

    /// `floor(self + 1/2)`.
    pub(crate) fn round(&self) -> Natural {
        let Some(places) = self.places() else {
            return self.shifted(0);
        };
        // floor((digits + 10^places / 2) / 10^places)
        let half = Natural::pow10(places - 1).mul_small(5);
        self.digits.add(&half).div_pow10(places)
    }

    pub(crate) fn is_above_one(&self) -> bool {
        match self.places() {
            Some(places) => self.digits > Natural::pow10(places),
            None => self.shifted(0) > Natural::from(1),
        }
    }

    /// The decimal places, if the exponent is below 0.
    fn places(&self) -> Option<usize> {
        usize::try_from(-self.exponent)
            .ok()
            .filter(|&places| places > 0)
    }

    /// The digits for an exponent at most this one's: `self / 10^exponent`.
    fn shifted(&self, exponent: i64) -> Natural {
        // The exponents of the conversion are within a few thousand.
        let shift = usize::try_from(self.exponent - exponent).unwrap_or_default();
        self.digits.mul_pow10(shift)
    }
}

/// The value of a product or quotient below 2^64 as a `u64`.
fn low(value: u128) -> u64 {
    u64::try_from(value % u128::from(BASE)).unwrap_or_default()
}

fn small_pow10(exponent: usize) -> u64 {
    // exponent is below LIMB_DIGITS, so it fits in a u32.
    10_u64.pow(u32::try_from(exponent).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::{Decimal, Natural};

    const NUMBERS: [u128; 12] = [
        0,
        1,
        5,
        9,
        10,
        999_999_999,
        1_000_000_000,
        1_000_000_001,
        123_456_789_012_345_678,
        999_999_999_999_999_999,
        18_446_744_073_709_551_615,
        18_446_744_073_709_551_616_000_000,
    ];

    fn natural(value: u128) -> Natural {
        Natural::from_digits(&value.to_string())
    }

    #[test]
    fn naturals_agree_with_u128() {
        for a in NUMBERS {
            assert_eq!(natural(a).to_string(), a.to_string());
            for b in NUMBERS {
                assert_eq!(natural(a).add(&natural(b)), natural(a + b), "{a} + {b}");
                assert_eq!(
                    natural(a).saturating_sub(&natural(b)),
                    natural(a.saturating_sub(b)),
                    "{a} - {b}"
                );
                assert_eq!(natural(a).cmp(&natural(b)), a.cmp(&b), "{a} <=> {b}");
                if let Some(product) = a.checked_mul(b) {
                    assert_eq!(natural(a).mul(&natural(b)), natural(product), "{a} * {b}");
                }
                let small = u64::try_from(b % (u128::from(u64::MAX) + 1)).unwrap();
                if let Some(product) = a.checked_mul(u128::from(small)) {
                    assert_eq!(
                        natural(a).mul_small(small),
                        natural(product),
                        "{a} * {small}"
                    );
                }
                if small > 0 {
                    assert_eq!(
                        natural(a).div_small(small),
                        natural(a / u128::from(small)),
                        "{a} / {small}"
                    );
                }
            }
            for exponent in [0, 1, 8, 9, 10, 18, 20] {
                let power = 10_u128.pow(exponent);
                let exponent = usize::try_from(exponent).unwrap();
                assert_eq!(natural(a).div_pow10(exponent), natural(a / power));
                if let Some(product) = a.checked_mul(power) {
                    assert_eq!(natural(a).mul_pow10(exponent), natural(product));
                }
            }
        }
    }

    #[test]
    fn naturals_go_beyond_u128() {
        let big = Natural::pow10(100).add(&Natural::from(7));
        let expected = format!("1{}7", "0".repeat(99));
        assert_eq!(big.to_string(), expected);
        assert_eq!(
            big.mul(&big).to_string(),
            format!("1{}14{}49", "0".repeat(98), "0".repeat(98))
        );
        assert_eq!(big.div_pow10(99).to_string(), "10");
        assert_eq!(big.div_pow10(1000), Natural::default());
        assert_eq!(Natural::from_digits("000"), Natural::default());
        assert_eq!(Natural::from(0).to_string(), "0");
        assert_eq!(Natural::default().mul_pow10(5), Natural::default());
    }

    /// The value of a decimal as the digits and exponent it was read with.
    fn read(text: &str) -> (String, i64) {
        let decimal = Decimal::parse(text).expect("a number text");
        (decimal.digits.to_string(), decimal.exponent)
    }

    #[test]
    fn decimals_read_the_number_form_of_section_13() {
        assert_eq!(read("0"), ("0".to_owned(), -1));
        assert_eq!(read("12"), ("120".to_owned(), -1));
        assert_eq!(read("0.25"), ("25".to_owned(), -2));
        assert_eq!(read("1e-05"), ("10".to_owned(), -6));
        assert_eq!(read("1.5e+07"), ("15".to_owned(), 6));
        assert_eq!(read("2e+999"), ("20".to_owned(), 998));
        assert_eq!(read("3.5e-999"), ("35".to_owned(), -1000));
        for text in [
            "",
            ".5",
            "5.",
            "05",
            "00",
            "1.2.3",
            "1e5",
            "1e+5",
            "1e+1000",
            "1E+05",
            "1e+05e+05",
            "1e05",
            "+1",
            "-1",
            "1e-+05",
            "1e+0a",
            "1_0",
            "１",
        ] {
            assert_eq!(Decimal::parse(text), None, "{text:?}");
        }
    }

    fn decimal(text: &str) -> Decimal {
        Decimal::parse(text).unwrap()
    }

    #[test]
    fn decimals_add_multiply_and_round_exactly() {
        let sum = decimal("1.5e+07").add(&decimal("0.25"));
        assert_eq!(sum.round().to_string(), "15000000");
        assert_eq!(decimal("0.5").round().to_string(), "1");
        assert_eq!(decimal("0.49").round().to_string(), "0");
        assert_eq!(decimal("2.5").round().to_string(), "3");
        assert_eq!(decimal("1.5e+07").round().to_string(), "15000000");
        let product = decimal("0.0333").mul(&decimal("1.5e+07"));
        assert_eq!(product.round().to_string(), "499500");
        let tiny = decimal("0.4").add(&decimal("1e-999"));
        assert_eq!(tiny.round().to_string(), "0");
        let half = decimal("0.4999").add(&decimal("1e-04"));
        assert_eq!(half.round().to_string(), "1");
        assert_eq!(
            decimal("1e+40").round().to_string(),
            format!("1{}", "0".repeat(40))
        );
    }

    #[test]
    fn decimals_above_one() {
        for (text, above) in [
            ("0", false),
            ("1", false),
            ("1.0", false),
            ("1.000001", true),
            ("0.99", false),
            ("2", true),
            ("1e+00", false),
            ("1e+01", true),
            ("0.1e+01", false),
            ("10e-01", false),
            ("11e-01", true),
            ("0e+05", false),
        ] {
            assert_eq!(decimal(text).is_above_one(), above, "{text}");
        }
    }
}
