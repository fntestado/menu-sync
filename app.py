import os
import io
import contextlib
from werkzeug.utils import secure_filename
import threading
import builtins
from queue import Queue, Empty

from doordash_scraper import run as dd_run
from uploader.login   import login_orders
from uploader.main    import upload_to_orders, scrape_all_brand_locations, NotLoggedInError
from flask import (
    Flask, render_template, request, redirect,
    flash, send_from_directory, url_for, 
    stream_with_context, Response
)

app = Flask(__name__)
app.secret_key = "replace-this-with-a-real-secret"

# where our per-run CSV copies land
CSV_DIR = os.getcwd()

# where we temporarily store uploaded CSVs
UPLOAD_FOLDER = os.path.join(app.instance_path, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"csv"}

app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER
)

def allowed_file(filename):
    return (
        "." in filename and
        filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/scrape", methods=["GET", "POST"])
def scrape():
    if request.method == "POST":
        store_url = request.form["store_url"].strip()
        try:
            sheet_link, tab_name, csv_file = dd_run(store_url)
            download_url = url_for('download_file', filename=csv_file)
            flash(
                f"✅ Scraped into Sheet: "
                f"<a href='{sheet_link}' target='_blank'>{sheet_link}</a> "
                f"(tab “{tab_name}”)<br>"
                f"⬇️ <a href='{download_url}'>Download CSV</a>",
                "success"
            )
        except Exception as e:
            flash(f"❌ Scrape error: {e}", "danger")

    return render_template("scrape.html")

@app.route("/download/<path:filename>")
def download_file(filename):
    # Flask expects (directory, path)
    return send_from_directory(CSV_DIR, filename, as_attachment=True)

from flask import stream_with_context, Response

@app.route("/upload_stream", methods=["POST"])
def upload_stream():
    """
    Streams the output of upload_to_orders back to the client in real time.
    """
    # 1) validate & save the CSV as before
    file = request.files.get("csv_file")
    if not file or not allowed_file(file.filename):
        return "❌ No valid CSV uploaded\n", 400

    filename = secure_filename(file.filename)
    dst = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(dst)

    brand = request.form.get("brand", "").strip()
    raw_location = request.form.get("location", "").strip()
    # strip off the “Brand — ” prefix if present:
    parts = raw_location.rsplit("—", 1)
    location = parts[-1].strip() if len(parts) == 2 else raw_location

    if not brand or not location:
        return "❌ Please select both a brand and a location\n", 400

    # 2) set up a thread & queue to capture print()s
    log_queue = Queue()

    def worker():
        # Monkey-patch builtins.print to push into our queue
        original_print = builtins.print

        def queue_print(*args, **kwargs):
            msg = " ".join(str(a) for a in args)
            log_queue.put(msg + "\n")

        builtins.print = queue_print
        try:
            upload_to_orders(dst, brand=brand, location=location)
        except NotLoggedInError as e:
            log_queue.put(f"⚠️ {e}\n")
        except Exception as e:
            log_queue.put(f"❌ {e}\n")
        finally:
            builtins.print = original_print
            # signal completion
            log_queue.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    # 3) stream from the queue to the client
    def generate():
        while True:
            line = log_queue.get()
            if line is None:
                break
            yield line

    return Response(
        stream_with_context(generate()),
        mimetype="text/plain"
    )

@app.route("/upload", methods=["GET", "POST"])
def upload():
    # 1) Scrape brands & locations for the dropdowns
    try:
        brands_and_locs = scrape_all_brand_locations()
    except Exception as e:
        brands_and_locs = {}
        flash(f"⚠️ Couldn't load brands/locations: {e}", "warning")

    logs = []  # we'll fill this if there's a POST

    if request.method == "POST":
        action = request.form.get("action")

        if action == "login":
            try:
                login_orders()
                flash("✅ Logged in to Orders.co (cookies saved)", "success")
            except Exception as e:
                flash(f"❌ Login failed: {e}", "danger")
            return redirect(url_for("upload"))

        # handle CSV upload + brand/location selection
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash("❌ Please select a CSV file to upload", "danger")
            return redirect(request.url)
        if not allowed_file(file.filename):
            flash("❌ Only .csv files are allowed", "danger")
            return redirect(request.url)

        filename = secure_filename(file.filename)
        dst = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(dst)

        brand     = request.form.get("brand", "").strip()
        raw_loc   = request.form.get("location", "").strip()
        # strip off “Brand — ” prefix
        parts     = raw_loc.rsplit("—", 1)
        location  = parts[-1].strip() if len(parts) == 2 else raw_loc

        if not brand or not location:
            flash("❌ Please select both a brand and a location", "danger")
            return redirect(request.url)

        # capture all your prints into `logs`
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                upload_to_orders(dst, brand=brand, location=location)
            flash("✅ Menu uploaded to Orders.co!", "success")
        except NotLoggedInError as e:
            flash(f"❗ {e}", "warning")
        except Exception as e:
            flash(f"❌ Upload error: {e}", "danger")
        finally:
            buf.seek(0)
            # splitlines preserves order; you can also do buf.getvalue().split('\n')
            logs = buf.read().splitlines()

        # re-render instead of redirect so we can display logs
        return render_template(
            "upload.html",
            brands_and_locs=brands_and_locs,
            logs=logs
        )

    # GET or after a redirect
    return render_template(
        "upload.html",
        brands_and_locs=brands_and_locs,
        logs=logs
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)