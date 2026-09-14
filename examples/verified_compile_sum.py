"""Generate, prove, and natively compile an integer-array sum."""

from ai_functions import LeanSpec, VerifiedCompileConfig, ai_verified_compile


@ai_verified_compile(
    lean_spec=LeanSpec(
        proposition="result = values.foldl (fun total value => total + value) 0",
    ),
    config=VerifiedCompileConfig(mathlib_revision=None),
)
def sum_values(values: list[int]) -> int:
    """Return the sum of values."""


if __name__ == "__main__":
    print(sum_values([3, -2, 10, 4]))
    print(f"Checked theorem: {sum_values.lean_source_path}")
    print(f"Generated C: {sum_values.c_source_path}")
