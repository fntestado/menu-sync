from flask import (
    Flask, render_template, request, redirect,
    flash, send_from_directory, url_for
)
import os
from werkzeug.utils import secure_filename

from doordash_scraper import run as dd_run
from uploader.login   import login_orders
from uploader.main    import upload_to_orders, NotLoggedInError

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

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "login":
            try:
                login_orders()
                flash("✅ Logged in to Orders.co (cookies saved)", "success")
            except Exception as e:
                flash(f"❌ Login failed: {e}", "danger")
            return redirect(url_for("upload"))

        # handle CSV upload
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

        try:
            upload_to_orders(dst)
            flash("✅ Menu uploaded to Orders.co!", "success")
        except NotLoggedInError as e:
            flash(f"❗ {e}", "warning")
        except Exception as e:
            flash(f"❌ Upload error: {e}", "danger")

    return render_template("upload.html")

if __name__ == "__main__":
    app.run(debug=True, port=5000)