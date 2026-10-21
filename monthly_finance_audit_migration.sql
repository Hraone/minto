-- Minto: Monthly Finance Audit + persistent month-end reminders
-- Run once in Supabase SQL Editor. Safe to re-run.
-- This migration creates audit/reminder storage and an atomic RPC for saving
-- an audit with its single linked balance adjustment. It does not modify
-- original transactions or create adjustments until a user confirms an audit.

BEGIN;

-- Explicitly marks whether a transaction participates in account balance math.
-- Reconciliation rows can be safely voided without deleting historical entries.
ALTER TABLE public.transactions
    ADD COLUMN IF NOT EXISTS affects_source_balance boolean NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS public.monthly_finance_audits (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    source_id integer NOT NULL REFERENCES public.user_sources(id) ON DELETE RESTRICT,
    audit_month date NOT NULL CHECK (EXTRACT(DAY FROM audit_month) = 1),
    cutoff_date date NOT NULL,
    expected_balance numeric(18,2) NOT NULL,
    actual_balance numeric(18,2),
    difference numeric(18,2),
    note text,
    status text NOT NULL DEFAULT 'not_started'
        CHECK (status IN ('not_started','needs_actual_balance','matched','edited_after_match')),
    adjustment_entry_id integer REFERENCES public.entries(id) ON DELETE SET NULL,
    adjustment_transaction_id integer REFERENCES public.transactions(id) ON DELETE SET NULL,
    confirmed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, source_id, audit_month),
    CHECK (actual_balance IS NULL OR actual_balance >= 0),
    CHECK (difference IS NULL OR actual_balance IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS monthly_finance_audits_user_month_idx
    ON public.monthly_finance_audits(user_id, audit_month DESC, source_id);

ALTER TABLE public.monthly_finance_audits ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own monthly finance audits" ON public.monthly_finance_audits;
CREATE POLICY "own monthly finance audits" ON public.monthly_finance_audits
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

CREATE TABLE IF NOT EXISTS public.monthly_finance_audit_reminders (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    reminder_month date NOT NULL CHECK (EXTRACT(DAY FROM reminder_month) = 1),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','snoozed','suppressed','completed')),
    action text,
    snooze_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, reminder_month)
);

CREATE INDEX IF NOT EXISTS monthly_finance_audit_reminders_due_idx
    ON public.monthly_finance_audit_reminders(user_id, reminder_month, status);

ALTER TABLE public.monthly_finance_audit_reminders ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own monthly finance audit reminders" ON public.monthly_finance_audit_reminders;
CREATE POLICY "own monthly finance audit reminders" ON public.monthly_finance_audit_reminders
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- Atomically save an audit and create/update/void its linked adjustment.
-- Adjustment transactions use category='transfer' so they affect the source
-- balance but do not inflate income, expense, investment, or category totals.
-- For credit cards, a positive difference means higher outstanding debt and
-- therefore records an 'out' transaction; savings/cash use 'in' for positive.
CREATE OR REPLACE FUNCTION public.save_monthly_finance_audit(
    p_source_id integer,
    p_audit_month date,
    p_cutoff_date date,
    p_expected_balance numeric,
    p_actual_balance numeric,
    p_note text DEFAULT NULL
) RETURNS public.monthly_finance_audits
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_uid uuid := auth.uid();
    v_source public.user_sources%ROWTYPE;
    v_audit public.monthly_finance_audits%ROWTYPE;
    v_diff numeric(18,2);
    v_direction text;
    v_entry_id integer;
    v_txn_id integer;
    v_entry_text text;
    v_status text;
BEGIN
    IF v_uid IS NULL THEN
        RAISE EXCEPTION 'Authentication required' USING ERRCODE = '28000';
    END IF;
    IF p_audit_month IS NULL OR EXTRACT(DAY FROM p_audit_month) <> 1 THEN
        RAISE EXCEPTION 'audit_month must be the first day of a calendar month';
    END IF;
    IF p_cutoff_date < p_audit_month
       OR p_cutoff_date >= (p_audit_month + INTERVAL '1 month')::date THEN
        RAISE EXCEPTION 'cutoff_date must fall inside audit_month';
    END IF;
    IF p_expected_balance IS NULL OR p_actual_balance IS NULL OR p_actual_balance < 0 THEN
        RAISE EXCEPTION 'Expected balance and a non-negative actual balance are required';
    END IF;

    SELECT * INTO v_source
    FROM public.user_sources
    WHERE id = p_source_id AND user_id = v_uid AND active = TRUE
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Account not found or not accessible' USING ERRCODE = '42501';
    END IF;

    v_diff := round(p_actual_balance - p_expected_balance, 2);
    v_status := CASE
        WHEN v_diff = 0 AND EXISTS (
            SELECT 1 FROM public.monthly_finance_audits
            WHERE user_id = v_uid AND source_id = p_source_id AND audit_month = p_audit_month
        ) THEN 'edited_after_match'
        WHEN v_diff = 0 THEN 'matched'
        ELSE 'matched'
    END;

    INSERT INTO public.monthly_finance_audits (
        user_id, source_id, audit_month, cutoff_date, expected_balance,
        actual_balance, difference, note, status, confirmed_at, updated_at
    ) VALUES (
        v_uid, p_source_id, p_audit_month, p_cutoff_date, round(p_expected_balance,2),
        round(p_actual_balance,2), v_diff, NULLIF(btrim(p_note), ''),
        CASE WHEN v_diff = 0 THEN 'matched' ELSE 'matched' END, now(), now()
    )
    ON CONFLICT (user_id, source_id, audit_month)
    DO UPDATE SET
        cutoff_date = EXCLUDED.cutoff_date,
        expected_balance = EXCLUDED.expected_balance,
        actual_balance = EXCLUDED.actual_balance,
        difference = EXCLUDED.difference,
        note = EXCLUDED.note,
        status = CASE
            WHEN public.monthly_finance_audits.status = 'matched'
                 AND public.monthly_finance_audits.actual_balance IS DISTINCT FROM EXCLUDED.actual_balance
            THEN 'edited_after_match'
            ELSE 'matched'
        END,
        confirmed_at = now(),
        updated_at = now()
    RETURNING * INTO v_audit;

    v_entry_id := v_audit.adjustment_entry_id;
    v_txn_id := v_audit.adjustment_transaction_id;

    IF v_diff = 0 THEN
        -- Keep history, but void the generated balance effect. Do not delete
        -- the entry or affect any user-created transaction.
        IF v_txn_id IS NOT NULL THEN
            UPDATE public.transactions
            SET affects_source_balance = FALSE
            WHERE id = v_txn_id AND user_id = v_uid;
        END IF;
        RETURN v_audit;
    END IF;

    v_direction := CASE
        WHEN v_source.source_type = 'credit_card' THEN
            CASE WHEN v_diff > 0 THEN 'out' ELSE 'in' END
        ELSE
            CASE WHEN v_diff > 0 THEN 'in' ELSE 'out' END
    END;
    v_entry_text := 'Balance Reconciliation: ' || v_source.name || ' · ' || to_char(p_audit_month, 'YYYY-MM');

    IF v_entry_id IS NULL OR v_txn_id IS NULL THEN
        INSERT INTO public.entries(user_id, entry_text, mode)
        VALUES (v_uid, v_entry_text, 'manual')
        RETURNING id INTO v_entry_id;

        INSERT INTO public.transactions(
            id, user_id, direction, category, source_id, amount, currency,
            description, raw_text, transaction_date, affects_source_balance
        ) VALUES (
            v_entry_id, v_uid, v_direction, 'transfer', p_source_id, abs(v_diff),
            'INR', 'Balance Reconciliation', v_entry_text, p_cutoff_date, TRUE
        ) RETURNING id INTO v_txn_id;

        UPDATE public.monthly_finance_audits
        SET adjustment_entry_id = v_entry_id,
            adjustment_transaction_id = v_txn_id,
            updated_at = now()
        WHERE id = v_audit.id
        RETURNING * INTO v_audit;
    ELSE
        UPDATE public.entries
        SET entry_text = v_entry_text
        WHERE id = v_entry_id AND user_id = v_uid;

        UPDATE public.transactions
        SET direction = v_direction,
            category = 'transfer',
            source_id = p_source_id,
            amount = abs(v_diff),
            description = 'Balance Reconciliation',
            raw_text = v_entry_text,
            transaction_date = p_cutoff_date,
            affects_source_balance = TRUE
        WHERE id = v_txn_id AND user_id = v_uid;

        UPDATE public.monthly_finance_audits
        SET updated_at = now()
        WHERE id = v_audit.id
        RETURNING * INTO v_audit;
    END IF;

    RETURN v_audit;
END;
$$;

-- Persist reminder choices by user and month. This RPC is idempotent.
CREATE OR REPLACE FUNCTION public.set_monthly_finance_audit_reminder(
    p_reminder_month date,
    p_action text,
    p_snooze_hours integer DEFAULT 24
) RETURNS public.monthly_finance_audit_reminders
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_uid uuid := auth.uid();
    v_status text;
    v_snooze timestamptz;
    v_row public.monthly_finance_audit_reminders%ROWTYPE;
BEGIN
    IF v_uid IS NULL THEN
        RAISE EXCEPTION 'Authentication required' USING ERRCODE = '28000';
    END IF;
    IF p_reminder_month IS NULL OR EXTRACT(DAY FROM p_reminder_month) <> 1 THEN
        RAISE EXCEPTION 'reminder_month must be the first day of a calendar month';
    END IF;
    IF p_action NOT IN ('later','suppress','complete','pending') THEN
        RAISE EXCEPTION 'Unsupported reminder action';
    END IF;
    v_status := CASE p_action
        WHEN 'later' THEN 'snoozed'
        WHEN 'suppress' THEN 'suppressed'
        WHEN 'complete' THEN 'completed'
        ELSE 'pending'
    END;
    v_snooze := CASE WHEN p_action = 'later'
        THEN now() + make_interval(hours => greatest(1, least(coalesce(p_snooze_hours,24), 168)))
        ELSE NULL END;

    INSERT INTO public.monthly_finance_audit_reminders(
        user_id, reminder_month, status, action, snooze_until, updated_at
    ) VALUES (v_uid, p_reminder_month, v_status, p_action, v_snooze, now())
    ON CONFLICT (user_id, reminder_month)
    DO UPDATE SET status = EXCLUDED.status, action = EXCLUDED.action,
                  snooze_until = EXCLUDED.snooze_until, updated_at = now()
    RETURNING * INTO v_row;
    RETURN v_row;
END;
$$;

GRANT EXECUTE ON FUNCTION public.save_monthly_finance_audit(integer,date,date,numeric,numeric,text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.set_monthly_finance_audit_reminder(date,text,integer) TO authenticated;

COMMIT;

-- After migration, verify:
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema='public' AND table_name IN
-- ('monthly_finance_audits','monthly_finance_audit_reminders');
