"""Dedicated Minto administration routes.

Registration is explicit so the existing Flask app can provide its trusted
authentication decorators and server-only Supabase service client.
"""
from datetime import datetime, timezone, timedelta

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from admin_validation import (
    ADMIN_FEEDBACK_PRIORITIES,
    ADMIN_FEEDBACK_STATUSES,
    parse_admin_page,
    validate_admin_feedback_update,
)
from feedback_validation import FEEDBACK_CATEGORIES

PAGE_SIZE = 25


def register_admin_routes(app, login_required, admin_required, admin_client):
    def user_directory(include_feedback_counts=False):
        service = admin_client()
        auth_users = []
        page = 1
        while page <= 1000:
            result = service.auth.admin.list_users(page=page, per_page=1000)
            batch = result.users if hasattr(result, "users") else getattr(result, "data", result)
            batch = batch or []
            auth_users.extend(batch)
            if len(batch) < 1000:
                break
            page += 1

        profiles = {}
        offset = 0
        while True:
            try:
                rows = service.table("profiles").select("id,display_name,is_deleted,phone").range(offset, offset + 999).execute().data or []
            except Exception as exc:
                app.logger.warning("Admin profile lookup unavailable (%s)", type(exc).__name__)
                break
            for row in rows:
                profiles[str(row.get("id"))] = row
            if len(rows) < 1000:
                break
            offset += 1000

        role_rows = service.table("admin_roles").select("user_id").execute().data or []
        admin_ids = {str(row.get("user_id")) for row in role_rows}
        feedback_counts = {}
        if include_feedback_counts:
            offset = 0
            while True:
                rows = service.table("user_feedback").select("user_id").range(offset, offset + 999).execute().data or []
                for row in rows:
                    uid = str(row.get("user_id"))
                    feedback_counts[uid] = feedback_counts.get(uid, 0) + 1
                if len(rows) < 1000:
                    break
                offset += 1000

        output = []
        for user in auth_users:
            uid = str(getattr(user, "id", "") or "")
            if not uid:
                continue
            profile = profiles.get(uid, {})
            if profile.get("is_deleted") is True:
                continue
            user_metadata = getattr(user, "user_metadata", None) or {}
            if not isinstance(user_metadata, dict):
                user_metadata = {}
            phone = (
                profile.get("phone")
                or getattr(user, "phone", None)
                or user_metadata.get("phone")
                or user_metadata.get("phone_number")
                or user_metadata.get("mobile")
                or user_metadata.get("mobile_number")
                or ""
            )
            output.append({
                "id": uid,
                "email": getattr(user, "email", None) or "",
                "phone": str(phone),
                "created_at": getattr(user, "created_at", None),
                "display_name": profile.get("display_name") or "",
                "role": "Administrator" if uid in admin_ids else "User",
                "feedback_count": feedback_counts.get(uid, 0),
            })
        output.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return output

    def parse_timestamp(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def audit_page(page):
        result = (
            admin_client().table("admin_activity_logs")
            .select("id,actor_user_id,target_type,target_id,action,outcome,details,created_at", count="exact")
            .order("created_at", desc=True)
            .range((page - 1) * PAGE_SIZE, page * PAGE_SIZE - 1)
            .execute()
        )
        total = result.count or 0
        return result.data or [], max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    @app.route("/admin")
    @login_required
    @admin_required
    def admin_dashboard():
        metrics = {key: None for key in (
            "total_users", "new_users_7d", "admin_count", "total_feedback",
            "awaiting_review", "in_progress", "unresolved_bugs",
        )}
        recent_activity, dashboard_error = [], None
        try:
            service = admin_client()
            users = user_directory()
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            metrics["total_users"] = len(users)
            metrics["new_users_7d"] = sum(
                1 for user in users
                if parse_timestamp(user.get("created_at")) and parse_timestamp(user.get("created_at")) >= cutoff
            )
            metrics["admin_count"] = service.table("admin_roles").select("user_id", count="exact").limit(1).execute().count or 0
            metrics["total_feedback"] = service.table("user_feedback").select("id", count="exact").limit(1).execute().count or 0
            metrics["awaiting_review"] = service.table("user_feedback").select("id", count="exact").in_("status", ["Submitted", "Under Review"]).limit(1).execute().count or 0
            metrics["in_progress"] = service.table("user_feedback").select("id", count="exact").eq("status", "In Progress").limit(1).execute().count or 0
            metrics["unresolved_bugs"] = service.table("user_feedback").select("id", count="exact").eq("category", "Bug Report").in_("status", ["Submitted", "Under Review", "In Progress"]).limit(1).execute().count or 0
            recent_activity = service.table("admin_activity_logs").select(
                "id,actor_user_id,target_type,target_id,action,outcome,created_at"
            ).order("created_at", desc=True).limit(8).execute().data or []
        except Exception as exc:
            app.logger.warning("Admin dashboard load failed (%s)", type(exc).__name__)
            dashboard_error = "Some admin data could not be loaded. Refresh the page to try again."
        return render_template("admin/dashboard.html", metrics=metrics, recent_activity=recent_activity, dashboard_error=dashboard_error)

    @app.route("/admin/users")
    @login_required
    @admin_required
    def admin_users():
        query = (request.args.get("q") or "").strip()[:100].lower()
        role_filter = request.args.get("role", "all")
        if role_filter not in ("all", "user", "admin"):
            role_filter = "all"
        page = parse_admin_page(request.args.get("page"), max_page=100000)
        users, users_error, total_pages = [], None, 1
        try:
            directory = user_directory(include_feedback_counts=True)
            if query:
                directory = [u for u in directory if query in u["id"].lower() or query in u["email"].lower() or query in u["phone"].lower() or query in u["display_name"].lower()]
            if role_filter == "user":
                directory = [u for u in directory if u["role"] == "User"]
            elif role_filter == "admin":
                directory = [u for u in directory if u["role"] == "Administrator"]
            total_pages = max(1, (len(directory) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, total_pages)
            users = directory[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        except Exception as exc:
            app.logger.warning("Admin user directory load failed (%s)", type(exc).__name__)
            users_error = "The user directory could not be loaded. Please retry."
        return render_template("admin/users.html", users=users, users_error=users_error, query=query, role_filter=role_filter, page=page, total_pages=total_pages, has_previous=page > 1, has_next=page < total_pages)

    @app.route("/admin/users/<uuid:target_user_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def admin_soft_delete_user(target_user_id):
        actor_id, target_id = str(session["user_id"]), str(target_user_id)
        if actor_id == target_id:
            flash("You cannot soft-delete your own account.")
            return redirect(url_for("admin_users", q=request.form.get("q", ""), role=request.form.get("role", "all"), page=request.form.get("page", "1")))
        try:
            service = admin_client()
            user_response = service.auth.admin.list_users(page=1, per_page=1000)
            # Supabase SDK versions differ: list_users may return a list directly
            # or a response object exposing .users.
            auth_users = user_response if isinstance(user_response, list) else getattr(user_response, "users", [])
            target = next((u for u in (auth_users or []) if str(getattr(u, "id", "")) == target_id), None)
            if target is None:
                flash("The selected user could not be found.")
            else:
                profile_rows = service.table("profiles").select("id,is_deleted").eq("id", target_id).limit(1).execute().data or []
                if not profile_rows:
                    flash("This user has no profile record to mark as deleted. No changes were made.")
                elif profile_rows[0].get("is_deleted") is True:
                    flash("This user is already soft-deleted.")
                else:
                    update_result = service.table("profiles").update({"is_deleted": True}).eq("id", target_id).execute()
                    updated_rows = getattr(update_result, "data", None) or []
                    if not updated_rows:
                        # Verify whether the update took effect in case the API omits
                        # representation data for this request.
                        verify_rows = service.table("profiles").select("id,is_deleted").eq("id", target_id).limit(1).execute().data or []
                        if not verify_rows or verify_rows[0].get("is_deleted") is not True:
                            raise RuntimeError("Profile soft-delete update returned no updated row; check profiles.is_deleted and service-role permissions.")
                    try:
                        service.table("admin_activity_logs").insert({
                            "actor_user_id": actor_id,
                            "target_type": "user",
                            "target_id": target_id,
                            "action": "soft_delete_user",
                            "outcome": "success",
                            "details": {"email": getattr(target, "email", None) or ""},
                        }).execute()
                    except Exception as log_exc:
                        app.logger.warning("Admin soft-delete audit log failed (%s)", type(log_exc).__name__)
                    flash("User soft-deleted. Their database records have not been physically removed.")
        except Exception as exc:
            app.logger.exception("Admin soft-delete failed for target %s", target_id)
            error_text = str(exc).lower()
            if "supabase_service_role_key is not configured" in error_text or "service role" in error_text or "service_role" in error_text:
                flash("Soft delete failed: configure SUPABASE_SERVICE_ROLE_KEY in Railway variables, then redeploy.")
            elif "is_deleted" in error_text and any(term in error_text for term in ("column", "schema cache", "does not exist", "could not find")):
                flash("Soft delete failed: the profiles.is_deleted column is missing. Run the soft-delete SQL migration in Supabase, then retry.")
            elif "permission" in error_text or "row-level security" in error_text or "rls" in error_text or "42501" in error_text:
                flash("Soft delete failed because the database denied the update. Check that the server is using the Supabase service-role key and that the key is current.")
            else:
                flash("Soft delete failed. Open Railway → service → Logs and check the 'Admin soft-delete failed' entry for the exact cause.")
        return redirect(url_for("admin_users", q=request.form.get("q", ""), role=request.form.get("role", "all"), page=request.form.get("page", "1")))

    @app.route("/admin/users/<uuid:target_user_id>/role", methods=["POST"])
    @login_required
    @admin_required
    def admin_change_user_role(target_user_id):
        action = request.form.get("action", "")
        if action not in ("grant", "revoke"):
            abort(400)
        actor_id, target_id = str(session["user_id"]), str(target_user_id)
        if action == "grant" and actor_id == target_id:
            flash("You cannot grant administrator access to your own account.")
            return redirect(url_for("admin_users"))
        try:
            result = admin_client().rpc("admin_set_role", {
                "p_target_user_id": target_id,
                "p_grant_access": action == "grant",
                "p_actor_user_id": actor_id,
            }).execute()
            payload = result.data[0] if isinstance(result.data, list) and result.data else result.data
            if not isinstance(payload, dict) or not payload.get("ok"):
                reason = payload.get("reason") if isinstance(payload, dict) else None
                if reason == "last_admin":
                    flash("This is the last administrator. Grant another administrator access before revoking this role.")
                elif reason == "not_found":
                    flash("The selected user could not be found.")
                else:
                    flash("The role change was not applied. Refresh the page and try again.")
            elif payload.get("changed"):
                flash("Administrator access granted." if action == "grant" else "Administrator access revoked.")
            else:
                flash("No role change was needed; the stored role is already up to date.")
        except Exception as exc:
            app.logger.warning("Admin role change failed (%s)", type(exc).__name__)
            flash("The role change could not be saved. No success was reported; please retry.")
        return redirect(url_for("admin_users", q=request.form.get("q", ""), role=request.form.get("role", "all"), page=request.form.get("page", "1")))

    @app.route("/admin/feedback")
    @login_required
    @admin_required
    def admin_feedback():
        page = parse_admin_page(request.args.get("page"), max_page=100000)
        status_filter = request.args.get("status", "all")
        category_filter = request.args.get("category", "all")
        priority_filter = request.args.get("priority", "all")
        query = (request.args.get("q") or "").strip()[:100]
        if status_filter not in ("all",) + ADMIN_FEEDBACK_STATUSES:
            status_filter = "all"
        if category_filter not in ("all",) + tuple(FEEDBACK_CATEGORIES):
            category_filter = "all"
        if priority_filter not in ("all",) + ADMIN_FEEDBACK_PRIORITIES:
            priority_filter = "all"
        rows, feedback_error, total_pages = [], None, 1
        try:
            service = admin_client()
            statement = service.table("user_feedback").select(
                "id,user_id,category,rating,message,status,priority,created_at,updated_at", count="exact"
            )
            if status_filter != "all":
                statement = statement.eq("status", status_filter)
            if category_filter != "all":
                statement = statement.eq("category", category_filter)
            if priority_filter != "all":
                statement = statement.eq("priority", priority_filter)
            if query:
                escaped = query.replace("%", "\\%").replace("_", "\\_")
                statement = statement.ilike("message", "%" + escaped + "%")
            result = statement.order("created_at", desc=True).range((page - 1) * PAGE_SIZE, page * PAGE_SIZE - 1).execute()
            total = result.count or 0
            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, total_pages)
            if page != parse_admin_page(request.args.get("page"), max_page=100000):
                result = statement.order("created_at", desc=True).range((page - 1) * PAGE_SIZE, page * PAGE_SIZE - 1).execute()
            rows = result.data or []
            index = {u["id"]: u for u in user_directory()}
            for item in rows:
                submitter = index.get(str(item.get("user_id")), {})
                item["submitter_email"] = submitter.get("email") or "Unknown account"
                item["submitter_name"] = submitter.get("display_name") or ""
        except Exception as exc:
            app.logger.warning("Admin feedback inbox load failed (%s)", type(exc).__name__)
            feedback_error = "Feedback could not be loaded. Please refresh and try again."
        return render_template(
            "admin/feedback.html", feedback_rows=rows, feedback_error=feedback_error,
            status_filter=status_filter, category_filter=category_filter, priority_filter=priority_filter,
            query=query, categories=FEEDBACK_CATEGORIES, statuses=ADMIN_FEEDBACK_STATUSES,
            priorities=ADMIN_FEEDBACK_PRIORITIES, page=page, total_pages=total_pages,
            has_previous=page > 1, has_next=page < total_pages,
        )

    @app.route("/admin/feedback/<int:feedback_id>", methods=["GET", "POST"])
    @login_required
    @admin_required
    def admin_feedback_detail(feedback_id):
        service = admin_client()
        item, feedback_error = None, None
        try:
            found = service.table("user_feedback").select(
                "id,user_id,category,rating,message,status,priority,created_at,updated_at"
            ).eq("id", feedback_id).limit(1).execute().data or []
            item = found[0] if found else None
            if item:
                submitter = next((u for u in user_directory() if u["id"] == str(item["user_id"])), {})
                item["submitter_email"] = submitter.get("email") or "Unknown account"
                item["submitter_name"] = submitter.get("display_name") or ""
        except Exception as exc:
            app.logger.warning("Admin feedback detail load failed (%s)", type(exc).__name__)
            feedback_error = "This feedback item could not be loaded."
        if not item:
            if feedback_error:
                return render_template("admin/feedback_detail.html", feedback_item=None, feedback_error=feedback_error), 503
            abort(404)
        if request.method == "POST":
            values, errors = validate_admin_feedback_update(request.form.get("status"), request.form.get("priority"))
            if errors:
                flash("Choose a supported status and priority.")
                return redirect(url_for("admin_feedback_detail", feedback_id=feedback_id))
            try:
                result = service.rpc("admin_update_feedback", {
                    "p_feedback_id": feedback_id, "p_status": values["status"],
                    "p_priority": values["priority"], "p_actor_user_id": str(session["user_id"]),
                }).execute()
                payload = result.data[0] if isinstance(result.data, list) and result.data else result.data
                if isinstance(payload, dict) and payload.get("ok"):
                    flash("Feedback workflow updated.")
                elif isinstance(payload, dict) and payload.get("reason") == "not_found":
                    abort(404)
                else:
                    flash("The feedback update was not saved. Please retry.")
            except HTTPException:
                raise
            except Exception as exc:
                app.logger.warning("Admin feedback update failed (%s)", type(exc).__name__)
                flash("The feedback update could not be saved. No success was reported.")
            return redirect(url_for("admin_feedback_detail", feedback_id=feedback_id))
        return render_template("admin/feedback_detail.html", feedback_item=item, feedback_error=feedback_error, statuses=ADMIN_FEEDBACK_STATUSES, priorities=ADMIN_FEEDBACK_PRIORITIES)

    @app.route("/admin/activity")
    @login_required
    @admin_required
    def admin_activity():
        page = parse_admin_page(request.args.get("page"), max_page=100000)
        logs, logs_error, total_pages = [], None, 1
        try:
            logs, total_pages = audit_page(page)
            page = min(page, total_pages)
            if page != parse_admin_page(request.args.get("page"), max_page=100000):
                logs, total_pages = audit_page(page)
        except Exception as exc:
            app.logger.warning("Admin activity log load failed (%s)", type(exc).__name__)
            logs_error = "Administrative activity logs could not be loaded."
        return render_template("admin/activity.html", logs=logs, logs_error=logs_error, page=page, total_pages=total_pages, has_previous=page > 1, has_next=page < total_pages)
