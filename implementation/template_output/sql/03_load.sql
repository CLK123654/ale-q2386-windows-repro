\copy ledger.account(account_id,account_name,currency,normal_side,active) FROM '__ACCOUNTS_CSV__' WITH(FORMAT CSV,HEADER TRUE)
\copy ledger.ledger_period(period_id,start_date,end_date,status) FROM '__PERIODS_CSV__' WITH(FORMAT CSV,HEADER TRUE)
