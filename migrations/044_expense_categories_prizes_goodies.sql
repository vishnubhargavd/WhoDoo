-- Two more kinds of expense, so an event's prizes and goodies show as their own lines in its
-- cash flow instead of sitting under "other". Additive: every existing claim still satisfies it.

ALTER TABLE expense_claims DROP CONSTRAINT IF EXISTS expense_claims_category_check;
ALTER TABLE expense_claims ADD CONSTRAINT expense_claims_category_check
    CHECK (category IN ('supplies', 'food', 'travel', 'printing', 'decoration', 'prizes', 'goodies', 'other'));
