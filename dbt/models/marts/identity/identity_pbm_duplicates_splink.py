"""Duplicate PBM cardholders (one person, two cardholder IDs) found with Splink.

The same Fellegi-Sunter comparisons as identity_matches_splink, but with
link_type dedupe_only on the PBM cardholders alone: a PBM can reissue a card
after re-enrollment and key the person again. Trained without labels, on an
in-memory DuckDB with one thread (see identity_matches_splink).

The m probabilities (how one person's records differ between two keyings) come
from the linkage of the PBM to the payer, trained here exactly as in
identity_matches_splink: that linkage has thousands of true pairs, the PBM's
duplicates under a hundred, too few for expectation maximisation to observe
every comparison level (Splink then falls back to default values). The u
probabilities and the prior are estimated on the PBM itself, from its random
pairs. Returns every scored pair with a match probability of at least 0.01, the
lower cardholder ID first; gold.pbm_persons decides which pairs are the same
person.
"""

import duckdb
import splink.comparison_level_library as cll
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

# Same first name comparison as identity_matches_splink (dbt Python models
# cannot import each other).
FIRST_NAME = cl.CustomComparison(
    output_column_name="first_name",
    comparison_levels=[
        cll.NullLevel("first_name"),
        cll.ExactMatchLevel("first_name", term_frequency_adjustments=True),
        cll.ExactMatchLevel("first_name_canonical").configure(label_for_charts="Nickname of the same name"),
        cll.JaroWinklerLevel("first_name", 0.92),
        cll.JaroWinklerLevel("first_name", 0.88),
        cll.JaroWinklerLevel("first_name", 0.7),
        cll.ElseLevel(),
    ],
)


COMPARISONS = [
    FIRST_NAME,
    cl.NameComparison("last_name").configure(term_frequency_adjustments=True),
    cl.DateOfBirthComparison("birth_date", input_is_string=False),
    cl.ExactMatch("gender"),
    cl.ExactMatch("zip_code"),
]
BLOCKING_RULES = [
    block_on("birth_date"),
    block_on("first_name_canonical", "last_name"),
    block_on("last_name", "zip_code"),
    block_on("first_name_canonical", "zip_code"),
]


def estimate_prior_and_u(linker) -> None:
    linker.training.estimate_probability_two_random_records_match(
        [block_on("first_name_canonical", "last_name", "birth_date")], recall=0.8
    )
    linker.training.estimate_u_using_random_sampling(max_pairs=2e6, seed=42)


def model(dbt, session):
    dbt.config(materialized="table")
    people = dbt.ref("identity_people").to_arrow_table()

    con = duckdb.connect(config={"threads": 1})
    con.register("people", people)
    # Sorted by ID, as in identity_matches_splink: the random samples depend on the
    # row order. The PBM twice: once as a side of the linkage, once on its own for the dedupe
    # (Splink needs a distinct table per registered input).
    for name, source in (("legacy_people", "legacy"), ("pbm_link_people", "pbm"), ("pbm_people", "pbm")):
        con.execute(f"""
            CREATE TABLE {name} AS
            SELECT * EXCLUDE (source_dataset) FROM people WHERE source_dataset = '{source}'
            ORDER BY unique_id
        """)
    db_api = DuckDBAPI(connection=con)

    # 1. The linkage of identity_matches_splink, trained the same way, for its m.
    link = Linker(
        [db_api.register("legacy_people", dataset_display_name="legacy"),
         db_api.register("pbm_link_people", dataset_display_name="pbm_link")],
        SettingsCreator(link_type="link_only", unique_id_column_name="unique_id",
                        comparisons=COMPARISONS, blocking_rules_to_generate_predictions=BLOCKING_RULES,
                        retain_intermediate_calculation_columns=False),
        log_level="WARNING",
    )
    estimate_prior_and_u(link)
    link.training.estimate_parameters_using_expectation_maximisation(
        block_on("birth_date"), record_sample_proportion=1.0
    )
    link.training.estimate_parameters_using_expectation_maximisation(
        block_on("last_name", "zip_code"), record_sample_proportion=1.0
    )

    # 2. The dedupe: the linkage's m, with u and the prior of the PBM's own pairs.
    trained = link.misc.save_model_to_json()
    trained["link_type"] = "dedupe_only"
    linker = Linker(db_api.register("pbm_people", dataset_display_name="pbm"), trained, log_level="WARNING")
    estimate_prior_and_u(linker)
    predictions = linker.inference.predict(threshold_match_probability=0.01)
    return con.sql(f"""
        SELECT
            least(unique_id_l, unique_id_r)    AS cardholder_id,
            greatest(unique_id_l, unique_id_r) AS other_cardholder_id,
            match_weight,
            match_probability,
            gamma_first_name,
            gamma_last_name,
            gamma_birth_date,
            gamma_gender,
            gamma_zip_code
        FROM {predictions.physical_name}
        ORDER BY cardholder_id, other_cardholder_id
    """).to_arrow_table()
