"""Small, testable helpers for reducing login-time Supabase round trips."""


def load_login_profile_state(client, user_id):
    """Read all login preferences in one request.

    Return None when the combined read fails (for example, on an older schema)
    so the caller can use its legacy per-column loaders. No tokens or secrets
    are logged here.
    """
    try:
        rows = (
            client.table("profiles")
            .select("app_mode,theme,genz_mode,display_name,biometric_enabled")
            .eq("id", user_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        return None

    return rows[0] if rows else {}
