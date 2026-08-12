CREATE FUNCTION ledger.guard_open_period()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_matches integer;
BEGIN
  SELECT count(*) INTO v_matches
  FROM ledger.ledger_period
  WHERE NEW.booked_on >= start_date
    AND NEW.booked_on < end_date
    AND status='OPEN';
  IF v_matches <> 1 THEN
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='PERIOD_NOT_OPEN';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER journal_period_guard
BEFORE INSERT ON ledger.journal_entry
FOR EACH ROW EXECUTE FUNCTION ledger.guard_open_period();

CREATE FUNCTION ledger.assert_entry()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_entry text;
  v_lines integer;
  v_debit numeric(18,2);
  v_credit numeric(18,2);
  v_currency_ok boolean;
  v_active_ok boolean;
BEGIN
  IF TG_TABLE_NAME='journal_entry' THEN
    v_entry := NEW.entry_id;
  ELSE
    v_entry := COALESCE(NEW.entry_id,OLD.entry_id);
  END IF;
  SELECT count(p.*),
         COALESCE(sum(p.amount) FILTER(WHERE p.side='D'),0),
         COALESCE(sum(p.amount) FILTER(WHERE p.side='C'),0),
         COALESCE(bool_and(a.currency=e.currency),false),
         COALESCE(bool_and(a.active),false)
  INTO v_lines,v_debit,v_credit,v_currency_ok,v_active_ok
  FROM ledger.journal_entry e
  LEFT JOIN ledger.posting p ON p.entry_id=e.entry_id
  LEFT JOIN ledger.account a ON a.account_id=p.account_id
  WHERE e.entry_id=v_entry
  GROUP BY e.entry_id;
  IF v_lines < 2 OR v_debit <> v_credit OR NOT v_currency_ok OR NOT v_active_ok THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ENTRY_COMMIT_CHECK_FAILED';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER journal_commit_check
AFTER INSERT ON ledger.journal_entry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION ledger.assert_entry();

CREATE CONSTRAINT TRIGGER posting_commit_check
AFTER INSERT ON ledger.posting
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION ledger.assert_entry();

CREATE FUNCTION ledger.post_entry(
  p_external_ref text,
  p_booked_on date,
  p_currency text,
  p_lines jsonb,
  p_reversal_of text DEFAULT NULL
) RETURNS TABLE(entry_id text,created boolean)
LANGUAGE plpgsql AS $$
DECLARE
  v_lines jsonb;
  v_payload jsonb;
  v_existing jsonb;
  v_inserted integer;
  v_entry text := 'JE-' || p_external_ref;
BEGIN
  SELECT jsonb_agg(jsonb_build_object(
    'line_no',x.line_no,'account_id',x.account_id,'side',x.side,
    'amount',x.amount,'memo',COALESCE(x.memo,'')) ORDER BY x.line_no)
  INTO v_lines
  FROM jsonb_to_recordset(p_lines)
  AS x(line_no integer,account_id text,side text,amount numeric(18,2),memo text);
  v_lines := COALESCE(v_lines,'[]'::jsonb);
  v_payload := jsonb_build_object(
    'booked_on',p_booked_on,'currency',p_currency,
    'reversal_of',p_reversal_of,'lines',v_lines);
  INSERT INTO ledger.request_payload(external_ref,payload)
  VALUES(p_external_ref,v_payload) ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS v_inserted=ROW_COUNT;
  IF v_inserted=0 THEN
    SELECT payload INTO v_existing FROM ledger.request_payload
    WHERE external_ref=p_external_ref;
    IF v_existing <> v_payload THEN
      RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='IDEMPOTENCY_CONFLICT';
    END IF;
    RETURN QUERY SELECT j.entry_id,false FROM ledger.journal_entry j
    WHERE j.external_ref=p_external_ref;
    RETURN;
  END IF;
  INSERT INTO ledger.journal_entry(entry_id,external_ref,booked_on,currency,reversal_of)
  VALUES(v_entry,p_external_ref,p_booked_on,p_currency,p_reversal_of);
  INSERT INTO ledger.posting(entry_id,line_no,account_id,side,amount,memo)
  SELECT v_entry,x.line_no,x.account_id,x.side,x.amount,x.memo
  FROM jsonb_to_recordset(v_lines)
  AS x(line_no integer,account_id text,side text,amount numeric(18,2),memo text);
  RETURN QUERY SELECT v_entry,true;
END $$;

CREATE FUNCTION ledger.reverse_entry(
  p_original_ref text,
  p_reversal_ref text,
  p_booked_on date
) RETURNS TABLE(entry_id text,created boolean)
LANGUAGE plpgsql AS $$
DECLARE
  v_original text;
  v_currency text;
  v_lines jsonb;
BEGIN
  SELECT j.entry_id,j.currency INTO STRICT v_original,v_currency
  FROM ledger.journal_entry j WHERE j.external_ref=p_original_ref;
  SELECT jsonb_agg(jsonb_build_object(
    'line_no',p.line_no,'account_id',p.account_id,
    'side',CASE p.side WHEN 'D' THEN 'C' ELSE 'D' END,
    'amount',p.amount,'memo','reversal ' || p.memo) ORDER BY p.line_no)
  INTO v_lines FROM ledger.posting p WHERE p.entry_id=v_original;
  RETURN QUERY SELECT * FROM ledger.post_entry(
    p_reversal_ref,p_booked_on,v_currency,v_lines,v_original);
END $$;

CREATE FUNCTION ledger.run_review_case(p_case_type text)
RETURNS TABLE(actual_outcome text,actual_sqlstate text,evidence text)
LANGUAGE plpgsql AS $$
DECLARE v_payload jsonb; v_original_count integer; v_entry text; v_created boolean;
BEGIN
  IF p_case_type='baseline_requests' THEN
    RETURN QUERY SELECT 'ALLOW'::text,''::text,
      ('entries=' || count(*))::text FROM ledger.journal_entry WHERE reversal_of IS NULL;
  ELSIF p_case_type='identical_retry' THEN
    SELECT payload INTO v_payload FROM ledger.request_payload WHERE external_ref='PAY-1001';
    SELECT p.entry_id,p.created INTO v_entry,v_created FROM ledger.post_entry(
      'PAY-1001',(v_payload->>'booked_on')::date,v_payload->>'currency',v_payload->'lines') AS p;
    RETURN QUERY SELECT 'ALLOW'::text,''::text,
      ('entry_id=' || v_entry || ';created=' || v_created::text)::text;
  ELSIF p_case_type='changed_retry' THEN
    BEGIN
      PERFORM * FROM ledger.post_entry('PAY-1001','2026-06-10','USD',
        '[{"line_no":1,"account_id":"A-CASH-USD","side":"D","amount":121,"memo":"changed"},{"line_no":2,"account_id":"A-REVENUE-USD","side":"C","amount":121,"memo":"changed"}]');
      RETURN QUERY SELECT 'ALLOW'::text,''::text,'unexpected allow'::text;
    EXCEPTION WHEN OTHERS THEN
      RETURN QUERY SELECT 'DENY'::text,SQLSTATE::text,SQLERRM::text;
    END;
  ELSIF p_case_type='unbalanced_entry' THEN
    BEGIN
      PERFORM * FROM ledger.post_entry('BAD-UNBALANCED','2026-06-12','USD',
        '[{"line_no":1,"account_id":"A-CASH-USD","side":"D","amount":10},{"line_no":2,"account_id":"A-REVENUE-USD","side":"C","amount":9}]');
      SET CONSTRAINTS ALL IMMEDIATE;
      RETURN QUERY SELECT 'ALLOW'::text,''::text,'unexpected allow'::text;
    EXCEPTION WHEN OTHERS THEN
      RETURN QUERY SELECT 'DENY'::text,SQLSTATE::text,SQLERRM::text;
    END;
  ELSIF p_case_type='inactive_account' THEN
    BEGIN
      PERFORM * FROM ledger.post_entry('BAD-INACTIVE','2026-06-12','USD',
        '[{"line_no":1,"account_id":"A-OLD-USD","side":"D","amount":10},{"line_no":2,"account_id":"A-REVENUE-USD","side":"C","amount":10}]');
      SET CONSTRAINTS ALL IMMEDIATE;
      RETURN QUERY SELECT 'ALLOW'::text,''::text,'unexpected allow'::text;
    EXCEPTION WHEN OTHERS THEN
      RETURN QUERY SELECT 'DENY'::text,SQLSTATE::text,SQLERRM::text;
    END;
  ELSIF p_case_type='cross_currency' THEN
    BEGIN
      PERFORM * FROM ledger.post_entry('BAD-CURRENCY','2026-06-12','USD',
        '[{"line_no":1,"account_id":"A-CASH-USD","side":"D","amount":10},{"line_no":2,"account_id":"A-PAYABLE-EUR","side":"C","amount":10}]');
      SET CONSTRAINTS ALL IMMEDIATE;
      RETURN QUERY SELECT 'ALLOW'::text,''::text,'unexpected allow'::text;
    EXCEPTION WHEN OTHERS THEN
      RETURN QUERY SELECT 'DENY'::text,SQLSTATE::text,SQLERRM::text;
    END;
  ELSIF p_case_type='closed_period' THEN
    BEGIN
      PERFORM * FROM ledger.post_entry('BAD-PERIOD','2026-05-15','USD',
        '[{"line_no":1,"account_id":"A-CASH-USD","side":"D","amount":10},{"line_no":2,"account_id":"A-REVENUE-USD","side":"C","amount":10}]');
      RETURN QUERY SELECT 'ALLOW'::text,''::text,'unexpected allow'::text;
    EXCEPTION WHEN OTHERS THEN
      RETURN QUERY SELECT 'DENY'::text,SQLSTATE::text,SQLERRM::text;
    END;
  ELSIF p_case_type='reversal_mirror' THEN
    RETURN QUERY
    SELECT CASE WHEN count(*)>0 AND bool_and(r.account_id=o.account_id AND r.amount=o.amount AND r.side<>o.side)
                THEN 'ALLOW' ELSE 'DENY' END::text,''::text,('lines=' || count(*))::text
    FROM ledger.journal_entry j
    JOIN ledger.journal_entry rev ON rev.reversal_of=j.entry_id
    JOIN ledger.posting o ON o.entry_id=j.entry_id
    JOIN ledger.posting r ON r.entry_id=rev.entry_id AND r.line_no=o.line_no
    WHERE j.external_ref='PAY-1001';
  ELSIF p_case_type='duplicate_reversal' THEN
    BEGIN
      PERFORM * FROM ledger.reverse_entry('PAY-1001','REV-SECOND','2026-06-21');
      SET CONSTRAINTS ALL IMMEDIATE;
      RETURN QUERY SELECT 'ALLOW'::text,''::text,'unexpected allow'::text;
    EXCEPTION WHEN OTHERS THEN
      RETURN QUERY SELECT 'DENY'::text,SQLSTATE::text,SQLERRM::text;
    END;
  ELSE
    RETURN QUERY SELECT 'DENY'::text,'22023'::text,'unknown review case'::text;
  END IF;
END $$;
