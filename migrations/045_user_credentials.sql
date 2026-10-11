-- Sign-in with a username and password. One row per person who can sign in; `users` itself is
-- not altered. The password is never stored: only a salted scrypt hash of it.

CREATE TABLE user_credentials (
    user_id        uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    username       text NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,39}$'),
    password_hash  text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX user_credentials_username_uq ON user_credentials (username);
