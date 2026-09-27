 from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
    jsonify
)

from config import Config

import db

from services.budget_service import (
    CATEGORIES,
    month_now,
    budget_summary
)

from services.gemini_service import GeminiService


# -------------------------------------------------------
# Application
# -------------------------------------------------------

app = Flask(__name__)

app.config.from_object(Config)

# Create database tables
db.init_db()

# Initialize AI service
ai = GeminiService()


# -------------------------------------------------------
# Global template variables
# -------------------------------------------------------

@app.context_processor
def globals_for_templates():

    return {
        "categories": CATEGORIES,
        "current_month": month_now(),
        "ai_available": ai.available
    }


# -------------------------------------------------------
# Home
# -------------------------------------------------------

@app.get("/")
def index():

    return render_template(
        "index.html"
    )


# -------------------------------------------------------
# Dashboard
# -------------------------------------------------------

@app.get("/dashboard")
def dashboard():

    month = request.args.get(
        "month",
        month_now()
    )

    transactions = db.list_transactions()

    spending = db.monthly_spending(
        month
    )

    total = db.total_spending(
        month
    )

    budgets = budget_summary(
        month
    )

    return render_template(
        "dashboard.html",
        transactions=transactions,
        spending=spending,
        total=total,
        budgets=budgets,
        month=month
    )


# -------------------------------------------------------
# Manual transaction
# -------------------------------------------------------

@app.post("/transactions")
def create_transaction():

    description = request.form.get(
        "description",
        ""
    ).strip()

    raw_amount = request.form.get(
        "amount",
        ""
    ).strip()

    category = request.form.get(
        "category",
        "Other"
    )

    # Validate amount
    try:

        amount = float(
            raw_amount
        )

        if amount <= 0:
            raise ValueError

    except ValueError:

        flash(
            "Enter a valid positive amount.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )

    # Validate description
    if not description:

        flash(
            "Enter a description.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )

    db.add_transaction(
        description,
        amount,
        category
    )

    flash(
        "Expense added successfully.",
        "success"
    )

    return redirect(
        url_for("dashboard")
    )


# -------------------------------------------------------
# AI transaction
# -------------------------------------------------------

@app.post("/transactions/ai")
def create_ai_transaction():

    text = request.form.get(
        "text",
        ""
    ).strip()

    if not text:

        flash(
            "Enter an expense such as 'Coffee 120'.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )

    try:

        parsed = ai.parse_expense(
            text
        )

        db.add_transaction(
            parsed.description,
            parsed.amount,
            parsed.category
        )

        flash(
            (
                f"AI added "
                f"₹{parsed.amount:,.2f} "
                f"as {parsed.category}."
            ),
            "success"
        )

    except Exception as exc:

        flash(
            f"Could not parse the entry: {exc}",
            "error"
        )

    return redirect(
        url_for("dashboard")
    )


# -------------------------------------------------------
# Save budget
# -------------------------------------------------------

@app.post("/budgets")
def save_budget():

    category = request.form.get(
        "category",
        "Other"
    )

    month = request.form.get(
        "month",
        month_now()
    )

    try:

        amount = float(
            request.form.get(
                "amount",
                "0"
            )
        )

        if amount < 0:
            raise ValueError

        db.upsert_budget(
            category,
            amount,
            month
        )

        flash(
            "Budget saved successfully.",
            "success"
        )

    except ValueError:

        flash(
            "Enter a valid non-negative budget.",
            "error"
        )

    return redirect(
        url_for(
            "dashboard",
            month=month
        )
    )


# -------------------------------------------------------
# AI recommendations
# -------------------------------------------------------

@app.get("/recommendations")
def recommendations():

    month = request.args.get(
        "month",
        month_now()
    )

    total = db.total_spending(
        month
    )

    rows = budget_summary(
        month
    )

    try:

        result = ai.recommendations(
            month,
            total,
            rows
        )

        return render_template(
            "recommendations.html",
            result=result,
            month=month,
            total=total
        )

    except Exception as exc:

        flash(
            f"Recommendation service error: {exc}",
            "error"
        )

        return redirect(
            url_for(
                "dashboard",
                month=month
            )
        )


# -------------------------------------------------------
# API health check
# -------------------------------------------------------

@app.get("/api/health")
def health():

    return jsonify(
        {
            "status": "ok",
            "gemini_configured": ai.available
        }
    )


# -------------------------------------------------------
# API: parse expense
# -------------------------------------------------------

@app.post("/api/parse-expense")
def api_parse_expense():

    payload = request.get_json(
        silent=True
    ) or {}

    text = str(
        payload.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return jsonify(
            {
                "error": "text is required"
            }
        ), 400

    try:

        result = ai.parse_expense(
            text
        )

        return jsonify(
            result.model_dump()
        )

    except Exception as exc:

        return jsonify(
            {
                "error": str(exc)
            }
        ), 400


# -------------------------------------------------------
# Application entry point
# -------------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )