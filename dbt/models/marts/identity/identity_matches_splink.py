"""Probabilistic matching of PBM cardholders to legacy members with Splink.

A Fellegi-Sunter model (Splink, on an in-memory DuckDB) scores every candidate
pair that shares a birth date, a full name, or a last name or first name plus ZIP
code. It is trained without labels: u probabilities from random pairs (fixed
seed) and m probabilities by expectation maximisation. The ground truth is not
used here. Returns every scored pair with a match probability of at least 0.01;
gold.member_xref decides which ones to link.
"""

import duckdb
import splink.comparison_level_library as cll
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

# First names: exact, then the same formal name through the nickname seed, then
# decreasing Jaro-Winkler similarity. The nickname level keeps true matches that
# use a nickname from teaching the model that a wholly different first name is
# common among matches.
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


def model(dbt, session):
    dbt.config(materialized="table")
    people = dbt.ref("identity_people").to_arrow_table()

    # Splink's working tables stay out of the lakehouse. One thread: with several,
    # parallel sums add up in varying order and the scores differ in the 15th digit
    # from run to run; with one, the output is identical on every run.
    con = duckdb.connect(config={"threads": 1})
    con.register("people", people)
    for source in ("legacy", "pbm"):
        con.execute(f"""
            CREATE TABLE {source}_people AS
            SELECT * EXCLUDE (source_dataset) FROM people WHERE source_dataset = '{source}'
        """)
    db_api = DuckDBAPI(connection=con)
    inputs = [db_api.register(f"{source}_people", dataset_display_name=source) for source in ("legacy", "pbm")]

    settings = SettingsCreator(
        link_type="link_only",
        unique_id_column_name="unique_id",
        comparisons=[
            FIRST_NAME,
            cl.NameComparison("last_name").configure(term_frequency_adjustments=True),
            cl.DateOfBirthComparison("birth_date", input_is_string=False),
            cl.ExactMatch("gender"),
            cl.ExactMatch("zip_code"),
        ],
        blocking_rules_to_generate_predictions=[
            block_on("birth_date"),
            block_on("first_name_canonical", "last_name"),
            block_on("last_name", "zip_code"),
            block_on("first_name_canonical", "zip_code"),
        ],
        retain_intermediate_calculation_columns=False,
    )
    linker = Linker(inputs, settings, log_level="WARNING")
    linker.training.estimate_probability_two_random_records_match(
        [block_on("first_name_canonical", "last_name", "birth_date")], recall=0.8
    )
    linker.training.estimate_u_using_random_sampling(max_pairs=2e6, seed=42)
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("birth_date"), record_sample_proportion=1.0
    )
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("last_name", "zip_code"), record_sample_proportion=1.0
    )
    predictions = linker.inference.predict(threshold_match_probability=0.01)

    return con.sql(f"""
        SELECT
            CASE WHEN source_dataset_l = 'pbm' THEN unique_id_l ELSE unique_id_r END AS cardholder_id,
            CASE WHEN source_dataset_l = 'pbm' THEN unique_id_r ELSE unique_id_l END AS member_id,
            match_weight,
            match_probability,
            gamma_first_name,
            gamma_last_name,
            gamma_birth_date,
            gamma_gender,
            gamma_zip_code
        FROM {predictions.physical_name}
        ORDER BY cardholder_id, match_probability DESC, member_id
    """).to_arrow_table()
