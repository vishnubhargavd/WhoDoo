-- PROPOSED, NOT DEPLOYED. Runner owns transactions; requires 020, 021 and 030.
-- Credentials remain bearer secrets: only their digest and authenticated ciphertext persist.
ALTER TABLE public.event_tickets ADD COLUMN credential_encrypted bytea;
ALTER TABLE public.event_tickets ADD CONSTRAINT event_tickets_ciphertext_ck
  CHECK (credential_encrypted IS NULL OR octet_length(credential_encrypted) BETWEEN 60 AND 256);
CREATE UNIQUE INDEX event_tickets_id_event_uq ON public.event_tickets (id, event_id);

CREATE TABLE public.gatesync_staff (
  event_id uuid NOT NULL,
  club_id uuid NOT NULL,
  user_id uuid NOT NULL,
  assigned_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, user_id),
  FOREIGN KEY (event_id, club_id) REFERENCES public.events (id, club_id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, club_id) REFERENCES public.users (id, club_id) ON DELETE RESTRICT,
  FOREIGN KEY (assigned_by, club_id) REFERENCES public.users (id, club_id) ON DELETE RESTRICT
);
CREATE INDEX gatesync_staff_user_club_idx ON public.gatesync_staff (user_id, club_id);

CREATE TABLE public.gatesync_attendance (
  ticket_id uuid PRIMARY KEY,
  event_id uuid NOT NULL,
  club_id uuid NOT NULL,
  scanned_by uuid NOT NULL,
  checked_in_at timestamptz NOT NULL,
  FOREIGN KEY (ticket_id, event_id) REFERENCES public.event_tickets (id, event_id) ON DELETE RESTRICT,
  FOREIGN KEY (event_id, club_id) REFERENCES public.events (id, club_id) ON DELETE RESTRICT,
  FOREIGN KEY (scanned_by, club_id) REFERENCES public.users (id, club_id) ON DELETE RESTRICT
);
CREATE INDEX gatesync_attendance_event_idx ON public.gatesync_attendance (event_id, checked_in_at);
CREATE FUNCTION public.gatesync_reject_attendance_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $body$
BEGIN
  RAISE EXCEPTION 'Attendance is immutable; preserve the original check-in evidence' USING ERRCODE = '55000';
END
$body$;
REVOKE ALL ON FUNCTION public.gatesync_reject_attendance_mutation() FROM PUBLIC;
CREATE TRIGGER gatesync_attendance_immutable BEFORE UPDATE OR DELETE ON public.gatesync_attendance
FOR EACH ROW EXECUTE FUNCTION public.gatesync_reject_attendance_mutation();
-- Backend-only tables; no browser Data API permissions are proposed.
REVOKE ALL ON public.gatesync_staff, public.gatesync_attendance FROM PUBLIC;
