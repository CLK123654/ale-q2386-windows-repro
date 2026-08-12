CREATE SCHEMA ledger;

CREATE TABLE ledger.account(
  account_id text PRIMARY KEY,
  account_name text NOT NULL,
  currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
  normal_side text NOT NULL CHECK(normal_side IN ('D','C')),
  active boolean NOT NULL
);

CREATE TABLE ledger.ledger_period(
  period_id text PRIMARY KEY,
  start_date date NOT NULL,
  end_date date NOT NULL,
  status text NOT NULL CHECK(status IN ('OPEN','CLOSED')),
  CHECK(start_date < end_date)
);

CREATE TABLE ledger.request_payload(
  external_ref text PRIMARY KEY,
  payload jsonb NOT NULL
);

CREATE TABLE ledger.journal_entry(
  entry_id text PRIMARY KEY,
  external_ref text NOT NULL UNIQUE,
  booked_on date NOT NULL,
  currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
  reversal_of text REFERENCES ledger.journal_entry(entry_id)
);

CREATE UNIQUE INDEX one_reversal_per_entry
ON ledger.journal_entry(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TABLE ledger.posting(
  entry_id text NOT NULL REFERENCES ledger.journal_entry(entry_id),
  line_no integer NOT NULL CHECK(line_no > 0),
  account_id text NOT NULL REFERENCES ledger.account(account_id),
  side text NOT NULL CHECK(side IN ('D','C')),
  amount numeric(18,2) NOT NULL CHECK(amount > 0),
  memo text NOT NULL DEFAULT '',
  PRIMARY KEY(entry_id,line_no)
);

CREATE TABLE ledger.review_result(
  case_id text PRIMARY KEY,
  case_type text NOT NULL,
  expected_outcome text NOT NULL,
  actual_outcome text NOT NULL,
  actual_sqlstate text NOT NULL,
  evidence text NOT NULL,
  result text NOT NULL CHECK(result IN ('PASS','FAIL'))
);
