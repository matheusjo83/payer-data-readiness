-- Legacy payer data warehouse (simulated).
-- Deliberately reproduces common legacy traits:
--   * cryptic abbreviated names
--   * dates stored as VARCHAR(8) 'YYYYMMDD' ('99991231' = open-ended)
--   * single-character status codes
--   * no foreign keys (referential integrity is not enforced)

CREATE TABLE pln (                -- health plans / products
    pln_id      VARCHAR(10) PRIMARY KEY,
    pln_nm      VARCHAR(60),
    lob_cd      CHAR(3),          -- COM, MCR (Medicare Advantage), MCD (Medicaid), CHP
    eff_dt      VARCHAR(8),
    term_dt     VARCHAR(8)
);

CREATE TABLE mbr_mstr (           -- member master (may contain duplicates!)
    mbr_sk      SERIAL PRIMARY KEY,
    mbr_id      VARCHAR(12),
    fst_nm      VARCHAR(40),
    lst_nm      VARCHAR(40),
    dob         VARCHAR(8),
    gndr_cd     CHAR(1),          -- M, F, U (and some invalid values)
    zip_cd      VARCHAR(10),
    st_cd       CHAR(2),
    upd_ts      TIMESTAMP
);

CREATE TABLE elig_span (          -- eligibility / coverage spans
    elig_sk     SERIAL PRIMARY KEY,
    mbr_id      VARCHAR(12),
    pln_id      VARCHAR(10),
    eff_dt      VARCHAR(8),
    term_dt     VARCHAR(8),
    upd_ts      TIMESTAMP
);

CREATE TABLE prv (                -- providers
    prv_id      VARCHAR(10) PRIMARY KEY,
    npi         VARCHAR(10),
    prv_nm      VARCHAR(80),
    spclty_cd   VARCHAR(4),
    st_cd       CHAR(2),
    upd_ts      TIMESTAMP
);

CREATE TABLE clm_hdr (            -- claim header
    clm_id       VARCHAR(15) PRIMARY KEY,
    mbr_id       VARCHAR(12),
    prv_id       VARCHAR(10),
    clm_typ_cd   CHAR(1),         -- P professional, I institutional, R pharmacy
    svc_from_dt  VARCHAR(8),
    svc_to_dt    VARCHAR(8),
    rcvd_dt      VARCHAR(8),
    adj_dt       VARCHAR(8),
    clm_stat_cd  CHAR(2),         -- PD paid, DN denied, PN pending, VD void
    tot_chrg_amt NUMERIC(12,2),
    tot_pd_amt   NUMERIC(12,2),
    ntwk_cd      CHAR(1),         -- I in network, O out of network
    bill_typ_cd  VARCHAR(4),      -- institutional: type of bill (0111 inpatient, 0131 outpatient)
    pos_cd       CHAR(2),         -- professional: CMS place of service
    days_sply    INTEGER,         -- pharmacy: days supply
    daw_cd       CHAR(1),         -- pharmacy: NCPDP dispense as written code
    rfl_nbr      INTEGER,         -- pharmacy: refill number (0 = original fill)
    rfl_auth     INTEGER,         -- pharmacy: refills authorized
    upd_ts       TIMESTAMP
);

CREATE TABLE clm_ln (             -- claim lines (no FK to header!)
    clm_id      VARCHAR(15),
    ln_nbr      INTEGER,
    proc_cd     VARCHAR(7),
    dx_cd       VARCHAR(8),
    units       INTEGER,
    chrg_amt    NUMERIC(12,2),
    pd_amt      NUMERIC(12,2),
    ndc_cd      VARCHAR(11),      -- pharmacy: NDC (proc_cd is empty)
    rev_cd      VARCHAR(4),       -- institutional: NUBC revenue code
    upd_ts      TIMESTAMP,
    PRIMARY KEY (clm_id, ln_nbr)
);

CREATE TABLE pa_req (             -- prior authorization requests
    pa_id       VARCHAR(15) PRIMARY KEY,
    mbr_id      VARCHAR(12),
    prv_id      VARCHAR(10),
    svc_cd      VARCHAR(7),
    urgnt_flg   CHAR(1),          -- Y expedited, N standard
    req_ts      TIMESTAMP,
    dcsn_ts     TIMESTAMP,        -- NULL while pending
    dcsn_cd     CHAR(1),          -- A approved, D denied, P pending
    upd_ts      TIMESTAMP
);
