import datetime
import os

import click
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request
from flask_fenrir import create_fenrir_bp, secure_app
from sqlmodel import Session, create_engine, select

load_dotenv()

app = Flask("velo-tracker")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

database_url = os.getenv("DATABASE_URL", "")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["DATABASE_URL"] = database_url

engine = create_engine(app.config["DATABASE_URL"], echo=False)

# Fenrir
app.register_blueprint(create_fenrir_bp(engine))
secure_app(app)

def get_session():
    return Session(engine)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.cli.command("sync")
@click.option("--since", default=None, help="Start date YYYY-MM-DD (default: 1 year ago)")
def cli_sync(since: str):
    """Sync cycling activities from Garmin Connect."""
    from cli import sync_activities

    cutoff = (
        datetime.date.fromisoformat(since)
        if since
        else datetime.date.today() - datetime.timedelta(days=7)
    )
    click.echo(f"Syncing from {cutoff}…")
    with get_session() as session:
        result = sync_activities(session, cutoff)
    click.echo(f"Done — synced: {result['synced']}, skipped (non-cycling): {result['skipped']}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _stats(activities):
    rides = len(activities)
    distance = sum(a.distance or 0 for a in activities) / 1000
    time = sum(a.moving_time or 0 for a in activities)
    elevation = sum(a.total_elevation_gain or 0 for a in activities)
    tss = sum(a.tss or 0 for a in activities)
    rpe_vals = [a.rpe for a in activities if a.rpe]
    avg_rpe = sum(rpe_vals) / len(rpe_vals) if rpe_vals else None

    by_type = {}
    for a in activities:
        by_type.setdefault(a.activity_type, []).append(a)
    type_breakdown = {}
    for t, acts in sorted(by_type.items()):
        t_rpe = [a.rpe for a in acts if a.rpe]
        type_breakdown[t] = dict(
            rides=len(acts),
            distance=sum(a.distance or 0 for a in acts) / 1000,
            time=sum(a.moving_time or 0 for a in acts),
            elevation=sum(a.total_elevation_gain or 0 for a in acts),
            tss=sum(a.tss or 0 for a in acts),
            avg_rpe=sum(t_rpe) / len(t_rpe) if t_rpe else None,
        )

    return dict(rides=rides, distance=distance, time=time,
                elevation=elevation, tss=tss, avg_rpe=avg_rpe,
                by_type=type_breakdown)


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    with get_session() as session:
        activities = session.exec(
            select(Activity).order_by(Activity.start_date.desc())
        ).all()

    by_week = {}
    by_month = {}
    for a in activities:
        iso = a.start_date.isocalendar()
        wk = (iso[0], iso[1])
        mo = (a.start_date.year, a.start_date.month)
        by_week.setdefault(wk, []).append(a)
        by_month.setdefault(mo, []).append(a)

    def _week_label(year, week):
        mon = datetime.date.fromisocalendar(year, week, 1)
        sun = datetime.date.fromisocalendar(year, week, 7)
        if mon.month == sun.month:
            return f"{mon.strftime('%b %-d')}–{sun.day} {sun.strftime('%Y')}"
        return f"{mon.strftime('%b %-d')} – {sun.strftime('%b %-d %Y')}"

    weeks = [{"key": k, "label": _week_label(*k), "stats": _stats(v)}
             for k, v in sorted(by_week.items(), reverse=True)]
    months = [{"key": k, "label": datetime.date(k[0], k[1], 1).strftime("%B %Y"), "stats": _stats(v)}
              for k, v in sorted(by_month.items(), reverse=True)]

    # Prepare chart data (reverse chronological order for display)
    sorted_weeks = sorted(by_week.items())
    chart_labels = []
    chart_distance = []
    chart_weekly_tss = []
    
    for (year, week), activities in sorted_weeks:
        # Calculate weekly distance in km
        weekly_distance = sum(a.distance or 0 for a in activities) / 1000
        # Calculate weekly TSS (not cumulative)
        weekly_tss = sum(a.tss or 0 for a in activities)
        
        chart_labels.append(_week_label(year, week))
        chart_distance.append(weekly_distance)
        chart_weekly_tss.append(weekly_tss)
    
    chart_data = {
        "labels": chart_labels,
        "distance": chart_distance,
        "weeklyTss": chart_weekly_tss
    }

    return render_template("dashboard.html", weeks=weeks, months=months, chart_data=chart_data)


@app.route("/activities")
def list_activities():
    with get_session() as session:
        activities = session.exec(
            select(Activity).order_by(Activity.start_date.desc())
        ).all()
    return render_template(
        "activities/list.html",
        activities=activities,
        today=datetime.date.today(),
        timedelta=datetime.timedelta,
    )


@app.route("/activities/sync", methods=["POST"])
def sync_activities_route():
    from cli import sync_activities

    oldest = request.form.get("since") or (
        datetime.date.today() - datetime.timedelta(days=7)
    ).isoformat()
    cutoff = datetime.date.fromisoformat(oldest)
    with get_session() as session:
        result = sync_activities(session, cutoff)
    return render_template("activities/_sync_result.html", **result)


@app.route("/activities/<string:garmin_id>")
def show_activity(garmin_id: str):
    with get_session() as session:
        activity = session.exec(
            select(Activity).where(Activity.garmin_id == garmin_id)
        ).first()
        if not activity:
            return "Not found", 404
    return render_template("activities/show.html", activity=activity)


@app.route("/activities/<string:garmin_id>/streams")
def activity_streams(garmin_id: str):
    from flask import jsonify

    with get_session() as session:
        activity = session.exec(
            select(Activity).where(Activity.garmin_id == garmin_id)
        ).first()
        if activity and activity.polyline:
            return jsonify(activity.polyline)
    return jsonify([])


@app.route("/activities/<string:garmin_id>/notes", methods=["POST"])
def save_notes(garmin_id: str):
    with get_session() as session:
        activity = session.exec(
            select(Activity).where(Activity.garmin_id == garmin_id)
        ).first()
        if not activity:
            return "Not found", 404
        activity.notes = request.form.get("notes", "").strip() or None
        activity.updated_at = datetime.datetime.utcnow()
        session.add(activity)
        session.commit()
        session.refresh(activity)
    return render_template("activities/_notes.html", activity=activity)


@app.route("/heatmap/data")
def heatmap_data():
    from flask import jsonify

    with get_session() as session:
        activities = session.exec(
            select(Activity).where(Activity.polyline.is_not(None))
        ).all()
    
    # Build heatmap data: coordinate frequency mapping
    coord_frequency = {}
    
    for activity in activities:
        if activity.polyline:
            for lat, lon in activity.polyline:
                # Round coordinates to reduce precision for frequency counting
                rounded_lat = round(lat, 4)  # ~11m precision
                rounded_lon = round(lon, 4)
                coord_key = (rounded_lat, rounded_lon)
                coord_frequency[coord_key] = coord_frequency.get(coord_key, 0) + 1
    
    # Convert to heatmap format: [lat, lon, intensity]
    if not coord_frequency:
        return jsonify([])
    
    # Normalize intensity values for better gradient display
    max_frequency = max(coord_frequency.values())
    heatmap_points = [
        [coord[0], coord[1], min(frequency / max_frequency, 1.0)] 
        for coord, frequency in coord_frequency.items()
    ]
    
    return jsonify(heatmap_points)


@app.route("/health")
def health():
    try:
        with get_session() as session:
            session.exec(select(Activity).limit(1)).first()
        return {"status": "healthy"}, 200
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}, 500


# Import models after engine is set up so Alembic can detect them
from models import Activity  # noqa: E402, F401
