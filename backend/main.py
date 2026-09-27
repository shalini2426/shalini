import os
import re
import sqlite3
import json
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

from google import genai
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "pocketsmart-development-secret"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

DATABASE = "pocketsmart.db"


app = Flask(__name__)

app.secret_key = SECRET_KEY


# ============================================================
# GEMINI SETUP
# ============================================================

gemini_client = None

if GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

    except Exception as error:

        print(
            "Gemini initialization failed:",
            error
        )


# ============================================================
# DATA MODELS
# ============================================================

class ExpenseResult(BaseModel):

    description: str = Field(
        description="Short expense description"
    )

    amount: float = Field(
        description="Expense amount"
    )

    category: str = Field(
        description="Expense category"
    )


class RecommendationResult(BaseModel):

    summary: str

    recommendations: list[str]


# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = [

    "Food",

    "Transport",

    "Shopping",

    "Entertainment",

    "Bills",

    "Education",

    "Health",

    "Travel",

    "Other"

]


# ============================================================
# LOCAL CATEGORY FALLBACK
# ============================================================

KEYWORDS = {

    "Food": [
        "food",
        "lunch",
        "dinner",
        "breakfast",
        "coffee",
        "restaurant",
        "pizza",
        "snack",
        "meal",
        "cafe"
    ],

    "Transport": [
        "bus",
        "train",
        "taxi",
        "uber",
        "metro",
        "petrol",
        "fuel",
        "diesel",
        "auto"
    ],

    "Shopping": [
        "shirt",
        "dress",
        "clothes",
        "shoes",
        "shopping",
        "amazon",
        "book",
        "gift"
    ],

    "Entertainment": [
        "movie",
        "cinema",
        "game",
        "music",
        "netflix",
        "concert"
    ],

    "Bills": [
        "bill",
        "electricity",
        "water",
        "internet",
        "phone",
        "rent",
        "recharge"
    ],

    "Education": [
        "school",
        "college",
        "course",
        "tuition",
        "class",
        "exam",
        "education"
    ],

    "Health": [
        "doctor",
        "medicine",
        "pharmacy",
        "hospital",
        "health",
        "medical"
    ],

    "Travel": [
        "travel",
        "hotel",
        "flight",
        "trip",
        "vacation",
        "tour"
    ]

}


def local_category(text):

    text = text.lower()

    for category, words in KEYWORDS.items():

        for word in words:

            if word in text:

                return category

    return "Other"


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


def initialize_database():

    db = get_db()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            description TEXT NOT NULL,

            amount REAL NOT NULL,

            category TEXT NOT NULL,

            created_at TEXT NOT NULL

        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            category TEXT NOT NULL,

            amount REAL NOT NULL,

            month TEXT NOT NULL,

            UNIQUE(category, month)

        )
        """
    )

    db.commit()

    db.close()


initialize_database()


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def add_expense(
    description,
    amount,
    category
):

    db = get_db()

    db.execute(
        """
        INSERT INTO expenses
        (
            description,
            amount,
            category,
            created_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            description,
            amount,
            category,
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    db.commit()

    db.close()


def get_expenses():

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM expenses

        ORDER BY id DESC
        """
    ).fetchall()

    db.close()

    return rows


def get_monthly_total(
    month
):

    db = get_db()

    row = db.execute(
        """
        SELECT
            COALESCE(
                SUM(amount),
                0
            ) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?
        """,
        (month,)
    ).fetchone()

    db.close()

    return float(
        row["total"]
    )


def get_category_spending(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT
            category,
            SUM(amount) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?

        GROUP BY category

        ORDER BY total DESC
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


def save_budget(
    category,
    amount,
    month
):

    db = get_db()

    db.execute(
        """
        INSERT INTO budgets
        (
            category,
            amount,
            month
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            category,
            month
        )

        DO UPDATE SET
            amount = excluded.amount
        """,
        (
            category,
            amount,
            month
        )
    )

    db.commit()

    db.close()


def get_budgets(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM budgets

        WHERE month = ?

        ORDER BY category
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


# ============================================================
# GEMINI EXPENSE PARSER
# ============================================================

def parse_expense_with_ai(
    text
):

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        amount_match = re.search(
            r"(\d+(?:\.\d+)?)",
            text
        )

        if not amount_match:

            raise ValueError(
                "Could not find an amount."
            )

        amount = float(
            amount_match.group(1)
        )

        description = re.sub(
            r"\b\d+(?:\.\d+)?\b",
            "",
            text
        ).strip()

        if not description:

            description = "Expense"

        category = local_category(
            description
        )

        return ExpenseResult(
            description=description,
            amount=amount,
            category=category
        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""
You are PocketSmart AI,
an expense categorization assistant.

Parse this expense:

{text}

Return:

description
amount
category

Allowed categories:

Food
Transport
Shopping
Entertainment
Bills
Education
Health
Travel
Other

Do not invent an amount.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                ExpenseResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return ExpenseResult.model_validate_json(
        response.text
    )


# ============================================================
# GEMINI RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    month
):

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    spending_data = {

        row["category"]:
            float(row["total"])

        for row in spending

    }

    budget_data = [

        {
            "category":
                row["category"],

            "budget":
                float(row["amount"]),

            "spent":
                spending_data.get(
                    row["category"],
                    0
                )
        }

        for row in budgets

    ]

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        recommendations = []

        for row in budget_data:

            remaining = (
                row["budget"]
                -
                row["spent"]
            )

            if remaining < 0:

                recommendations.append(

                    f"{row['category']} is "
                    f"₹{abs(remaining):,.2f} "
                    "over budget."

                )

            elif (
                row["budget"] > 0
                and
                row["spent"]
                /
                row["budget"]
                >= 0.8
            ):

                recommendations.append(

                    f"{row['category']} has "
                    "used more than 80% "
                    "of its budget."

                )

        if not recommendations:

            recommendations = [

                "Continue recording expenses regularly.",

                "Review your largest spending categories each week.",

                "Set realistic monthly budgets for your main categories."

            ]

        return RecommendationResult(

            summary=(
                f"Total spending for "
                f"{month}: "
                f"₹{total:,.2f}"
            ),

            recommendations=
                recommendations

        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""

You are PocketSmart AI,
a personal budgeting assistant.

Month:
{month}

Total spending:
₹{total:.2f}

Budget information:

{json.dumps(
    budget_data,
    indent=2
)}

Give practical and non-judgmental
budgeting suggestions.

Focus on:

- spending awareness
- category budgets
- expense organization
- everyday budgeting

Do not give investment,
tax, loan, or financial-product advice.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                RecommendationResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return RecommendationResult.model_validate_json(
        response.text
    )


# ============================================================
# API ROUTES
# ============================================================

@app.get("/api/health")
def api_health():

    return jsonify({

        "status": "ok",

        "gemini_configured":
            gemini_client is not None

    })


@app.post("/api/expense")
def api_expense():

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return jsonify({

            "success": False,

            "error":
                "Expense text is required."

        }), 400

    try:

        result = parse_expense_with_ai(
            text
        )

        add_expense(

            result.description,

            result.amount,

            result.category

        )

        return jsonify({

            "success": True,

            "expense":
                result.model_dump()

        })

    except Exception as error:

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


@app.post("/api/manual-expense")
def api_manual_expense():

    data = request.get_json(
        silent=True
    ) or {}

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid amount."

        }), 400

    if amount <= 0:

        return jsonify({

            "success": False,

            "error":
                "Amount must be positive."

        }), 400

    if not description:

        return jsonify({

            "success": False,

            "error":
                "Description is required."

        }), 400

    if category not in CATEGORIES:

        category = "Other"

    add_expense(

        description,

        amount,

        category

    )

    return jsonify({

        "success": True

    })


@app.get("/api/dashboard")
def api_dashboard():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    return jsonify({

        "month":
            month,

        "total":
            total,

        "spending": [

            {

                "category":
                    row["category"],

                "total":
                    float(row["total"])

            }

            for row in spending

        ],

        "budgets": [

            {

                "category":
                    row["category"],

                "amount":
                    float(row["amount"])

            }

            for row in budgets

        ]

    })


@app.post("/api/budget")
def api_budget():

    data = request.get_json(
        silent=True
    ) or {}

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    month = str(
        data.get(
            "month",
            datetime.now().strftime(
                "%Y-%m"
            )
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid budget."

        }), 400

    if category not in CATEGORIES:

        return jsonify({

            "success": False,

            "error":
                "Invalid category."

        }), 400

    if amount < 0:

        return jsonify({

            "success": False,

            "error":
                "Budget cannot be negative."

        }), 400

    save_budget(

        category,

        amount,

        month

    )

    return jsonify({

        "success": True

    })


@app.get("/api/recommendations")
def api_recommendations():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    try:

        result = generate_recommendations(
            month
        )

        return jsonify(
            result.model_dump()
        )

    except Exception as error:

        return jsonify({

            "error": str(error)

        }), 500


# ============================================================
# SINGLE-PAGE FRONTEND
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
    PocketSmart AI
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        #f5f7fb;

    color:
        #172033;

}

header {

    background:
        white;

    border-bottom:
        1px solid #e5e7eb;

    padding:
        18px 6%;

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

}

.logo {

    font-size:
        24px;

    font-weight:
        800;

}

.logo span {

    color:
        #6757d9;

}

nav {

    display:
        flex;

    gap:
        20px;

}

nav a {

    text-decoration:
        none;

    color:
        #555;

}

.container {

    max-width:
        1150px;

    margin:
        auto;

    padding:
        35px 20px;

}

.hero {

    padding:
        40px 0;

}

.hero h1 {

    font-size:
        48px;

    margin:
        10px 0;

}

.hero p {

    color:
        #687184;

    max-width:
        700px;

    line-height:
        1.7;

}

.grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                220px,
                1fr
            )
        );

    gap:
        18px;

}

.card {

    background:
        white;

    border:
        1px solid #e5e7eb;

    border-radius:
        16px;

    padding:
        22px;

    margin-bottom:
        20px;

}

.metric {

    font-size:
        30px;

    font-weight:
        800;

}

.metric small {

    display:
        block;

    color:
        #687184;

    font-size:
        13px;

    font-weight:
        normal;

}

input,
select {

    width:
        100%;

    padding:
        12px;

    margin:
        5px 0;

    border:
        1px solid #d7dce7;

    border-radius:
        8px;

    font-size:
        15px;

}

button {

    padding:
        12px 18px;

    border:
        none;

    border-radius:
        8px;

    background:
        #6757d9;

    color:
        white;

    font-weight:
        700;

    cursor:
        pointer;

    margin-top:
        8px;

}

button:hover {

    background:
        #5546c8;

}

table {

    width:
        100%;

    border-collapse:
        collapse;

}

th,
td {

    padding:
        12px;

    border-bottom:
        1px solid #eee;

    text-align:
        left;

}

.badge {

    background:
        #eeeaff;

    color:
        #6757d9;

    border-radius:
        20px;

    padding:
        4px 9px;

    font-size:
        12px;

}

.ai-result {

    background:
        #f1efff;

    border-radius:
        12px;

    padding:
        15px;

    margin-top:
        15px;

}

.recommendation {

    padding:
        15px;

    border-bottom:
        1px solid #eee;

}

.success {

    color:
        #15733d;

}

.error {

    color:
        #a32626;

}

footer {

    text-align:
        center;

    color:
        #777;

    padding:
        40px;

}

@media(max-width:700px) {

    .hero h1 {

        font-size:
            35px;

    }

    header {

        flex-direction:
            column;

        gap:
            15px;

    }

}

</style>

</head>


<body>


<header>

    <div class="logo">

        PocketSmart
        <span>AI</span>

    </div>


    <nav>

        <a href="#dashboard">
            Dashboard
        </a>

        <a href="#expenses">
            Expenses
        </a>

        <a href="#budgets">
            Budgets
        </a>

        <a href="#ai">
            AI Insights
        </a>

    </nav>

</header>


<div class="container">


<section class="hero">

    <h1>
        Smart Budgeting
        with AI
    </h1>

    <p>

        Track your expenses,
        create budgets,
        categorize spending,
        and get AI-powered
        recommendations.

    </p>

</section>


<section id="dashboard">

<h2>
    Dashboard
</h2>


<div class="grid">


<div class="card">

    <div class="metric">
        ₹<span id="total">0</span>
    </div>

    <small>
        Monthly spending
    </small>

</div>


<div class="card">

    <div class="metric">
        <span id="transactionCount">
            0
        </span>
    </div>

    <small>
        Transactions
    </small>

</div>


<div class="card">

    <div
        class="metric"
        id="aiStatus"
    >
        Checking...
    </div>

    <small>
        AI status
    </small>

</div>


</div>

</section>


<section id="expenses">


<div class="grid">


<div class="card">

<h2>
    Add Expense
</h2>


<form id="manualForm">

<input
    id="description"
    placeholder="Description"
    required
>


<input
    id="amount"
    type="number"
    step="0.01"
    min="0.01"
    placeholder="Amount"
    required
>


<select id="category">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<button>
    Add Expense
</button>

</form>

</div>


<div class="card">

<h2>
    AI Expense Entry
</h2>


<p>
    Example:
    <b>Lunch 180</b>
</p>


<form id="aiForm">

<input
    id="aiText"
    placeholder="Example: Coffee 120"
    required
>


<button>
    Add with AI
</button>

</form>


<div
    id="aiResult"
    class="ai-result"
    style="display:none"
></div>


</div>


</div>


<div class="card">

<h2>
    Recent Expenses
</h2>


<div id="expenseTable">
    Loading...
</div>


</div>


</section>


<section id="budgets">


<div class="card">

<h2>
    Monthly Budget
</h2>


<form id="budgetForm">


<select id="budgetCategory">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<input
    id="budgetAmount"
    type="number"
    step="0.01"
    min="0"
    placeholder="Budget amount"
    required
>


<button>
    Save Budget
</button>


</form>

</div>


</section>


<section id="ai">


<div class="card">

<h2>
    AI Recommendations
</h2>


<button
    onclick="loadRecommendations()"
>
    Generate Recommendations
</button>


<div
    id="recommendations"
    style="margin-top:20px"
></div>


</div>


</section>


</div>


<footer>

PocketSmart AI ·
Python · Flask · SQLite · Gemini

</footer>


<script>

async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            options
        );

    return response.json();

}


// ---------------------------------------------------------
// Load dashboard
// ---------------------------------------------------------

async function loadDashboard() {

    const data =
        await api(
            "/api/dashboard"
        );


    document.getElementById(
        "total"
    ).textContent =
        data.total.toFixed(2);


    const health =
        await api(
            "/api/health"
        );


    document.getElementById(
        "aiStatus"
    ).textContent =
        health.gemini_configured
            ? "Gemini Ready"
            : "Local AI";


    renderExpenses();

}


// ---------------------------------------------------------
// Expenses
// ---------------------------------------------------------

async function renderExpenses() {

    const data =
        await api(
            "/api/dashboard"
        );


    const rows =
        data.spending;


    let html = `

    <table>

        <thead>

            <tr>

                <th>
                    Category
                </th>

                <th>
                    Amount
                </th>

            </tr>

        </thead>

        <tbody>
    `;


    for (
        const row
        of rows
    ) {

        html += `

        <tr>

            <td>

                <span class="badge">
                    ${row.category}
                </span>

            </td>

            <td>
                ₹${row.total.toFixed(2)}
            </td>

        </tr>

        `;

    }


    html += `
        </tbody>
    </table>
    `;


    document.getElementById(
        "expenseTable"
    ).innerHTML =
        html;

}


// ---------------------------------------------------------
// Manual expense
// ---------------------------------------------------------

document.getElementById(
    "manualForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const response =
            await api(
                "/api/manual-expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            description:
                                document
                                .getElementById(
                                    "description"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "amount"
                                )
                                .value,

                            category:
                                document
                                .getElementById(
                                    "category"
                                )
                                .value

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Expense added successfully."
            );

            this.reset();

            loadDashboard();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// AI expense
// ---------------------------------------------------------

document.getElementById(
    "aiForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const text =
            document
            .getElementById(
                "aiText"
            )
            .value;


        const response =
            await api(
                "/api/expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            text: text
                        })

                }
            );


        const result =
            document.getElementById(
                "aiResult"
            );


        result.style.display =
            "block";


        if (
            response.success
        ) {

            result.innerHTML = `

                <b>
                    Expense added
                </b>

                <br><br>

                Description:
                ${response.expense.description}

                <br>

                Amount:
                ₹${response.expense.amount}

                <br>

                Category:
                ${response.expense.category}

            `;


            this.reset();

            loadDashboard();

        } else {

            result.innerHTML = `

                <span class="error">

                    ${response.error}

                </span>

            `;

        }

    }
);


// ---------------------------------------------------------
// Budget
// ---------------------------------------------------------

document.getElementById(
    "budgetForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const now =
            new Date();


        const month =
            now.getFullYear()
            +
            "-"
            +
            String(
                now.getMonth() + 1
            ).padStart(
                2,
                "0"
            );


        const response =
            await api(
                "/api/budget",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            category:
                                document
                                .getElementById(
                                    "budgetCategory"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "budgetAmount"
                                )
                                .value,

                            month:
                                month

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Budget saved."
            );

            this.reset();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// Recommendations
// ---------------------------------------------------------

async function loadRecommendations() {

    const box =
        document.getElementById(
            "recommendations"
        );


    box.innerHTML =
        "Generating AI recommendations...";


    const response =
        await api(
            "/api/recommendations"
        );


    if (
        response.error
    ) {

        box.innerHTML = `

            <p class="error">

                ${response.error}

            </p>

        `;

        return;

    }


    let html = `

        <h3>
            ${response.summary}
        </h3>

    `;


    response.recommendations
        .forEach(
            function(item, index) {

                html += `

                <div class="recommendation">

                    <b>
                        ${index + 1}.
                    </b>

                    ${item}

                </div>

                `;

            }
        );


    box.innerHTML =
        html;

}


// ---------------------------------------------------------
// Start
// ---------------------------------------------------------

loadDashboard();

</script>


</body>

</html>
"""


# ============================================================
# FRONTEND ROUTE
# ============================================================

@app.get("/")
def home():

    return render_template_string(
        HTML
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print(
        "=========================================="
    )

    print(
        "       PocketSmart AI"
    )

    print(
        "=========================================="
    )

    print(
        "Server:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print()

    print(
        "Gemini:",
        "Enabled"
        if gemini_client
        else "Local fallback"
    )

    print()

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True

    )import os
import re
import sqlite3
import json
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

from google import genai
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "pocketsmart-development-secret"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

DATABASE = "pocketsmart.db"


app = Flask(__name__)

app.secret_key = SECRET_KEY


# ============================================================
# GEMINI SETUP
# ============================================================

gemini_client = None

if GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

    except Exception as error:

        print(
            "Gemini initialization failed:",
            error
        )


# ============================================================
# DATA MODELS
# ============================================================

class ExpenseResult(BaseModel):

    description: str = Field(
        description="Short expense description"
    )

    amount: float = Field(
        description="Expense amount"
    )

    category: str = Field(
        description="Expense category"
    )


class RecommendationResult(BaseModel):

    summary: str

    recommendations: list[str]


# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = [

    "Food",

    "Transport",

    "Shopping",

    "Entertainment",

    "Bills",

    "Education",

    "Health",

    "Travel",

    "Other"

]


# ============================================================
# LOCAL CATEGORY FALLBACK
# ============================================================

KEYWORDS = {

    "Food": [
        "food",
        "lunch",
        "dinner",
        "breakfast",
        "coffee",
        "restaurant",
        "pizza",
        "snack",
        "meal",
        "cafe"
    ],

    "Transport": [
        "bus",
        "train",
        "taxi",
        "uber",
        "metro",
        "petrol",
        "fuel",
        "diesel",
        "auto"
    ],

    "Shopping": [
        "shirt",
        "dress",
        "clothes",
        "shoes",
        "shopping",
        "amazon",
        "book",
        "gift"
    ],

    "Entertainment": [
        "movie",
        "cinema",
        "game",
        "music",
        "netflix",
        "concert"
    ],

    "Bills": [
        "bill",
        "electricity",
        "water",
        "internet",
        "phone",
        "rent",
        "recharge"
    ],

    "Education": [
        "school",
        "college",
        "course",
        "tuition",
        "class",
        "exam",
        "education"
    ],

    "Health": [
        "doctor",
        "medicine",
        "pharmacy",
        "hospital",
        "health",
        "medical"
    ],

    "Travel": [
        "travel",
        "hotel",
        "flight",
        "trip",
        "vacation",
        "tour"
    ]

}


def local_category(text):

    text = text.lower()

    for category, words in KEYWORDS.items():

        for word in words:

            if word in text:

                return category

    return "Other"


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


def initialize_database():

    db = get_db()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            description TEXT NOT NULL,

            amount REAL NOT NULL,

            category TEXT NOT NULL,

            created_at TEXT NOT NULL

        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            category TEXT NOT NULL,

            amount REAL NOT NULL,

            month TEXT NOT NULL,

            UNIQUE(category, month)

        )
        """
    )

    db.commit()

    db.close()


initialize_database()


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def add_expense(
    description,
    amount,
    category
):

    db = get_db()

    db.execute(
        """
        INSERT INTO expenses
        (
            description,
            amount,
            category,
            created_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            description,
            amount,
            category,
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    db.commit()

    db.close()


def get_expenses():

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM expenses

        ORDER BY id DESC
        """
    ).fetchall()

    db.close()

    return rows


def get_monthly_total(
    month
):

    db = get_db()

    row = db.execute(
        """
        SELECT
            COALESCE(
                SUM(amount),
                0
            ) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?
        """,
        (month,)
    ).fetchone()

    db.close()

    return float(
        row["total"]
    )


def get_category_spending(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT
            category,
            SUM(amount) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?

        GROUP BY category

        ORDER BY total DESC
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


def save_budget(
    category,
    amount,
    month
):

    db = get_db()

    db.execute(
        """
        INSERT INTO budgets
        (
            category,
            amount,
            month
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            category,
            month
        )

        DO UPDATE SET
            amount = excluded.amount
        """,
        (
            category,
            amount,
            month
        )
    )

    db.commit()

    db.close()


def get_budgets(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM budgets

        WHERE month = ?

        ORDER BY category
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


# ============================================================
# GEMINI EXPENSE PARSER
# ============================================================

def parse_expense_with_ai(
    text
):

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        amount_match = re.search(
            r"(\d+(?:\.\d+)?)",
            text
        )

        if not amount_match:

            raise ValueError(
                "Could not find an amount."
            )

        amount = float(
            amount_match.group(1)
        )

        description = re.sub(
            r"\b\d+(?:\.\d+)?\b",
            "",
            text
        ).strip()

        if not description:

            description = "Expense"

        category = local_category(
            description
        )

        return ExpenseResult(
            description=description,
            amount=amount,
            category=category
        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""
You are PocketSmart AI,
an expense categorization assistant.

Parse this expense:

{text}

Return:

description
amount
category

Allowed categories:

Food
Transport
Shopping
Entertainment
Bills
Education
Health
Travel
Other

Do not invent an amount.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                ExpenseResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return ExpenseResult.model_validate_json(
        response.text
    )


# ============================================================
# GEMINI RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    month
):

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    spending_data = {

        row["category"]:
            float(row["total"])

        for row in spending

    }

    budget_data = [

        {
            "category":
                row["category"],

            "budget":
                float(row["amount"]),

            "spent":
                spending_data.get(
                    row["category"],
                    0
                )
        }

        for row in budgets

    ]

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        recommendations = []

        for row in budget_data:

            remaining = (
                row["budget"]
                -
                row["spent"]
            )

            if remaining < 0:

                recommendations.append(

                    f"{row['category']} is "
                    f"₹{abs(remaining):,.2f} "
                    "over budget."

                )

            elif (
                row["budget"] > 0
                and
                row["spent"]
                /
                row["budget"]
                >= 0.8
            ):

                recommendations.append(

                    f"{row['category']} has "
                    "used more than 80% "
                    "of its budget."

                )

        if not recommendations:

            recommendations = [

                "Continue recording expenses regularly.",

                "Review your largest spending categories each week.",

                "Set realistic monthly budgets for your main categories."

            ]

        return RecommendationResult(

            summary=(
                f"Total spending for "
                f"{month}: "
                f"₹{total:,.2f}"
            ),

            recommendations=
                recommendations

        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""

You are PocketSmart AI,
a personal budgeting assistant.

Month:
{month}

Total spending:
₹{total:.2f}

Budget information:

{json.dumps(
    budget_data,
    indent=2
)}

Give practical and non-judgmental
budgeting suggestions.

Focus on:

- spending awareness
- category budgets
- expense organization
- everyday budgeting

Do not give investment,
tax, loan, or financial-product advice.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                RecommendationResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return RecommendationResult.model_validate_json(
        response.text
    )


# ============================================================
# API ROUTES
# ============================================================

@app.get("/api/health")
def api_health():

    return jsonify({

        "status": "ok",

        "gemini_configured":
            gemini_client is not None

    })


@app.post("/api/expense")
def api_expense():

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return jsonify({

            "success": False,

            "error":
                "Expense text is required."

        }), 400

    try:

        result = parse_expense_with_ai(
            text
        )

        add_expense(

            result.description,

            result.amount,

            result.category

        )

        return jsonify({

            "success": True,

            "expense":
                result.model_dump()

        })

    except Exception as error:

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


@app.post("/api/manual-expense")
def api_manual_expense():

    data = request.get_json(
        silent=True
    ) or {}

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid amount."

        }), 400

    if amount <= 0:

        return jsonify({

            "success": False,

            "error":
                "Amount must be positive."

        }), 400

    if not description:

        return jsonify({

            "success": False,

            "error":
                "Description is required."

        }), 400

    if category not in CATEGORIES:

        category = "Other"

    add_expense(

        description,

        amount,

        category

    )

    return jsonify({

        "success": True

    })


@app.get("/api/dashboard")
def api_dashboard():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    return jsonify({

        "month":
            month,

        "total":
            total,

        "spending": [

            {

                "category":
                    row["category"],

                "total":
                    float(row["total"])

            }

            for row in spending

        ],

        "budgets": [

            {

                "category":
                    row["category"],

                "amount":
                    float(row["amount"])

            }

            for row in budgets

        ]

    })


@app.post("/api/budget")
def api_budget():

    data = request.get_json(
        silent=True
    ) or {}

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    month = str(
        data.get(
            "month",
            datetime.now().strftime(
                "%Y-%m"
            )
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid budget."

        }), 400

    if category not in CATEGORIES:

        return jsonify({

            "success": False,

            "error":
                "Invalid category."

        }), 400

    if amount < 0:

        return jsonify({

            "success": False,

            "error":
                "Budget cannot be negative."

        }), 400

    save_budget(

        category,

        amount,

        month

    )

    return jsonify({

        "success": True

    })


@app.get("/api/recommendations")
def api_recommendations():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    try:

        result = generate_recommendations(
            month
        )

        return jsonify(
            result.model_dump()
        )

    except Exception as error:

        return jsonify({

            "error": str(error)

        }), 500


# ============================================================
# SINGLE-PAGE FRONTEND
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
    PocketSmart AI
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        #f5f7fb;

    color:
        #172033;

}

header {

    background:
        white;

    border-bottom:
        1px solid #e5e7eb;

    padding:
        18px 6%;

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

}

.logo {

    font-size:
        24px;

    font-weight:
        800;

}

.logo span {

    color:
        #6757d9;

}

nav {

    display:
        flex;

    gap:
        20px;

}

nav a {

    text-decoration:
        none;

    color:
        #555;

}

.container {

    max-width:
        1150px;

    margin:
        auto;

    padding:
        35px 20px;

}

.hero {

    padding:
        40px 0;

}

.hero h1 {

    font-size:
        48px;

    margin:
        10px 0;

}

.hero p {

    color:
        #687184;

    max-width:
        700px;

    line-height:
        1.7;

}

.grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                220px,
                1fr
            )
        );

    gap:
        18px;

}

.card {

    background:
        white;

    border:
        1px solid #e5e7eb;

    border-radius:
        16px;

    padding:
        22px;

    margin-bottom:
        20px;

}

.metric {

    font-size:
        30px;

    font-weight:
        800;

}

.metric small {

    display:
        block;

    color:
        #687184;

    font-size:
        13px;

    font-weight:
        normal;

}

input,
select {

    width:
        100%;

    padding:
        12px;

    margin:
        5px 0;

    border:
        1px solid #d7dce7;

    border-radius:
        8px;

    font-size:
        15px;

}

button {

    padding:
        12px 18px;

    border:
        none;

    border-radius:
        8px;

    background:
        #6757d9;

    color:
        white;

    font-weight:
        700;

    cursor:
        pointer;

    margin-top:
        8px;

}

button:hover {

    background:
        #5546c8;

}

table {

    width:
        100%;

    border-collapse:
        collapse;

}

th,
td {

    padding:
        12px;

    border-bottom:
        1px solid #eee;

    text-align:
        left;

}

.badge {

    background:
        #eeeaff;

    color:
        #6757d9;

    border-radius:
        20px;

    padding:
        4px 9px;

    font-size:
        12px;

}

.ai-result {

    background:
        #f1efff;

    border-radius:
        12px;

    padding:
        15px;

    margin-top:
        15px;

}

.recommendation {

    padding:
        15px;

    border-bottom:
        1px solid #eee;

}

.success {

    color:
        #15733d;

}

.error {

    color:
        #a32626;

}

footer {

    text-align:
        center;

    color:
        #777;

    padding:
        40px;

}

@media(max-width:700px) {

    .hero h1 {

        font-size:
            35px;

    }

    header {

        flex-direction:
            column;

        gap:
            15px;

    }

}

</style>

</head>


<body>


<header>

    <div class="logo">

        PocketSmart
        <span>AI</span>

    </div>


    <nav>

        <a href="#dashboard">
            Dashboard
        </a>

        <a href="#expenses">
            Expenses
        </a>

        <a href="#budgets">
            Budgets
        </a>

        <a href="#ai">
            AI Insights
        </a>

    </nav>

</header>


<div class="container">


<section class="hero">

    <h1>
        Smart Budgeting
        with AI
    </h1>

    <p>

        Track your expenses,
        create budgets,
        categorize spending,
        and get AI-powered
        recommendations.

    </p>

</section>


<section id="dashboard">

<h2>
    Dashboard
</h2>


<div class="grid">


<div class="card">

    <div class="metric">
        ₹<span id="total">0</span>
    </div>

    <small>
        Monthly spending
    </small>

</div>


<div class="card">

    <div class="metric">
        <span id="transactionCount">
            0
        </span>
    </div>

    <small>
        Transactions
    </small>

</div>


<div class="card">

    <div
        class="metric"
        id="aiStatus"
    >
        Checking...
    </div>

    <small>
        AI status
    </small>

</div>


</div>

</section>


<section id="expenses">


<div class="grid">


<div class="card">

<h2>
    Add Expense
</h2>


<form id="manualForm">

<input
    id="description"
    placeholder="Description"
    required
>


<input
    id="amount"
    type="number"
    step="0.01"
    min="0.01"
    placeholder="Amount"
    required
>


<select id="category">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<button>
    Add Expense
</button>

</form>

</div>


<div class="card">

<h2>
    AI Expense Entry
</h2>


<p>
    Example:
    <b>Lunch 180</b>
</p>


<form id="aiForm">

<input
    id="aiText"
    placeholder="Example: Coffee 120"
    required
>


<button>
    Add with AI
</button>

</form>


<div
    id="aiResult"
    class="ai-result"
    style="display:none"
></div>


</div>


</div>


<div class="card">

<h2>
    Recent Expenses
</h2>


<div id="expenseTable">
    Loading...
</div>


</div>


</section>


<section id="budgets">


<div class="card">

<h2>
    Monthly Budget
</h2>


<form id="budgetForm">


<select id="budgetCategory">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<input
    id="budgetAmount"
    type="number"
    step="0.01"
    min="0"
    placeholder="Budget amount"
    required
>


<button>
    Save Budget
</button>


</form>

</div>


</section>


<section id="ai">


<div class="card">

<h2>
    AI Recommendations
</h2>


<button
    onclick="loadRecommendations()"
>
    Generate Recommendations
</button>


<div
    id="recommendations"
    style="margin-top:20px"
></div>


</div>


</section>


</div>


<footer>

PocketSmart AI ·
Python · Flask · SQLite · Gemini

</footer>


<script>

async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            options
        );

    return response.json();

}


// ---------------------------------------------------------
// Load dashboard
// ---------------------------------------------------------

async function loadDashboard() {

    const data =
        await api(
            "/api/dashboard"
        );


    document.getElementById(
        "total"
    ).textContent =
        data.total.toFixed(2);


    const health =
        await api(
            "/api/health"
        );


    document.getElementById(
        "aiStatus"
    ).textContent =
        health.gemini_configured
            ? "Gemini Ready"
            : "Local AI";


    renderExpenses();

}


// ---------------------------------------------------------
// Expenses
// ---------------------------------------------------------

async function renderExpenses() {

    const data =
        await api(
            "/api/dashboard"
        );


    const rows =
        data.spending;


    let html = `

    <table>

        <thead>

            <tr>

                <th>
                    Category
                </th>

                <th>
                    Amount
                </th>

            </tr>

        </thead>

        <tbody>
    `;


    for (
        const row
        of rows
    ) {

        html += `

        <tr>

            <td>

                <span class="badge">
                    ${row.category}
                </span>

            </td>

            <td>
                ₹${row.total.toFixed(2)}
            </td>

        </tr>

        `;

    }


    html += `
        </tbody>
    </table>
    `;


    document.getElementById(
        "expenseTable"
    ).innerHTML =
        html;

}


// ---------------------------------------------------------
// Manual expense
// ---------------------------------------------------------

document.getElementById(
    "manualForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const response =
            await api(
                "/api/manual-expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            description:
                                document
                                .getElementById(
                                    "description"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "amount"
                                )
                                .value,

                            category:
                                document
                                .getElementById(
                                    "category"
                                )
                                .value

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Expense added successfully."
            );

            this.reset();

            loadDashboard();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// AI expense
// ---------------------------------------------------------

document.getElementById(
    "aiForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const text =
            document
            .getElementById(
                "aiText"
            )
            .value;


        const response =
            await api(
                "/api/expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            text: text
                        })

                }
            );


        const result =
            document.getElementById(
                "aiResult"
            );


        result.style.display =
            "block";


        if (
            response.success
        ) {

            result.innerHTML = `

                <b>
                    Expense added
                </b>

                <br><br>

                Description:
                ${response.expense.description}

                <br>

                Amount:
                ₹${response.expense.amount}

                <br>

                Category:
                ${response.expense.category}

            `;


            this.reset();

            loadDashboard();

        } else {

            result.innerHTML = `

                <span class="error">

                    ${response.error}

                </span>

            `;

        }

    }
);


// ---------------------------------------------------------
// Budget
// ---------------------------------------------------------

document.getElementById(
    "budgetForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const now =
            new Date();


        const month =
            now.getFullYear()
            +
            "-"
            +
            String(
                now.getMonth() + 1
            ).padStart(
                2,
                "0"
            );


        const response =
            await api(
                "/api/budget",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            category:
                                document
                                .getElementById(
                                    "budgetCategory"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "budgetAmount"
                                )
                                .value,

                            month:
                                month

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Budget saved."
            );

            this.reset();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// Recommendations
// ---------------------------------------------------------

async function loadRecommendations() {

    const box =
        document.getElementById(
            "recommendations"
        );


    box.innerHTML =
        "Generating AI recommendations...";


    const response =
        await api(
            "/api/recommendations"
        );


    if (
        response.error
    ) {

        box.innerHTML = `

            <p class="error">

                ${response.error}

            </p>

        `;

        return;

    }


    let html = `

        <h3>
            ${response.summary}
        </h3>

    `;


    response.recommendations
        .forEach(
            function(item, index) {

                html += `

                <div class="recommendation">

                    <b>
                        ${index + 1}.
                    </b>

                    ${item}

                </div>

                `;

            }
        );


    box.innerHTML =
        html;

}


// ---------------------------------------------------------
// Start
// ---------------------------------------------------------

loadDashboard();

</script>


</body>

</html>
"""


# ============================================================
# FRONTEND ROUTE
# ============================================================

@app.get("/")
def home():

    return render_template_string(
        HTML
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print(
        "=========================================="
    )

    print(
        "       PocketSmart AI"
    )

    print(
        "=========================================="
    )

    print(
        "Server:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print()

    print(
        "Gemini:",
        "Enabled"
        if gemini_client
        else "Local fallback"
    )

    print()

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True

    )import os
import re
import sqlite3
import json
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

from google import genai
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "pocketsmart-development-secret"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

DATABASE = "pocketsmart.db"


app = Flask(__name__)

app.secret_key = SECRET_KEY


# ============================================================
# GEMINI SETUP
# ============================================================

gemini_client = None

if GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

    except Exception as error:

        print(
            "Gemini initialization failed:",
            error
        )


# ============================================================
# DATA MODELS
# ============================================================

class ExpenseResult(BaseModel):

    description: str = Field(
        description="Short expense description"
    )

    amount: float = Field(
        description="Expense amount"
    )

    category: str = Field(
        description="Expense category"
    )


class RecommendationResult(BaseModel):

    summary: str

    recommendations: list[str]


# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = [

    "Food",

    "Transport",

    "Shopping",

    "Entertainment",

    "Bills",

    "Education",

    "Health",

    "Travel",

    "Other"

]


# ============================================================
# LOCAL CATEGORY FALLBACK
# ============================================================

KEYWORDS = {

    "Food": [
        "food",
        "lunch",
        "dinner",
        "breakfast",
        "coffee",
        "restaurant",
        "pizza",
        "snack",
        "meal",
        "cafe"
    ],

    "Transport": [
        "bus",
        "train",
        "taxi",
        "uber",
        "metro",
        "petrol",
        "fuel",
        "diesel",
        "auto"
    ],

    "Shopping": [
        "shirt",
        "dress",
        "clothes",
        "shoes",
        "shopping",
        "amazon",
        "book",
        "gift"
    ],

    "Entertainment": [
        "movie",
        "cinema",
        "game",
        "music",
        "netflix",
        "concert"
    ],

    "Bills": [
        "bill",
        "electricity",
        "water",
        "internet",
        "phone",
        "rent",
        "recharge"
    ],

    "Education": [
        "school",
        "college",
        "course",
        "tuition",
        "class",
        "exam",
        "education"
    ],

    "Health": [
        "doctor",
        "medicine",
        "pharmacy",
        "hospital",
        "health",
        "medical"
    ],

    "Travel": [
        "travel",
        "hotel",
        "flight",
        "trip",
        "vacation",
        "tour"
    ]

}


def local_category(text):

    text = text.lower()

    for category, words in KEYWORDS.items():

        for word in words:

            if word in text:

                return category

    return "Other"


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


def initialize_database():

    db = get_db()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            description TEXT NOT NULL,

            amount REAL NOT NULL,

            category TEXT NOT NULL,

            created_at TEXT NOT NULL

        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            category TEXT NOT NULL,

            amount REAL NOT NULL,

            month TEXT NOT NULL,

            UNIQUE(category, month)

        )
        """
    )

    db.commit()

    db.close()


initialize_database()


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def add_expense(
    description,
    amount,
    category
):

    db = get_db()

    db.execute(
        """
        INSERT INTO expenses
        (
            description,
            amount,
            category,
            created_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            description,
            amount,
            category,
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    db.commit()

    db.close()


def get_expenses():

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM expenses

        ORDER BY id DESC
        """
    ).fetchall()

    db.close()

    return rows


def get_monthly_total(
    month
):

    db = get_db()

    row = db.execute(
        """
        SELECT
            COALESCE(
                SUM(amount),
                0
            ) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?
        """,
        (month,)
    ).fetchone()

    db.close()

    return float(
        row["total"]
    )


def get_category_spending(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT
            category,
            SUM(amount) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?

        GROUP BY category

        ORDER BY total DESC
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


def save_budget(
    category,
    amount,
    month
):

    db = get_db()

    db.execute(
        """
        INSERT INTO budgets
        (
            category,
            amount,
            month
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            category,
            month
        )

        DO UPDATE SET
            amount = excluded.amount
        """,
        (
            category,
            amount,
            month
        )
    )

    db.commit()

    db.close()


def get_budgets(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM budgets

        WHERE month = ?

        ORDER BY category
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


# ============================================================
# GEMINI EXPENSE PARSER
# ============================================================

def parse_expense_with_ai(
    text
):

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        amount_match = re.search(
            r"(\d+(?:\.\d+)?)",
            text
        )

        if not amount_match:

            raise ValueError(
                "Could not find an amount."
            )

        amount = float(
            amount_match.group(1)
        )

        description = re.sub(
            r"\b\d+(?:\.\d+)?\b",
            "",
            text
        ).strip()

        if not description:

            description = "Expense"

        category = local_category(
            description
        )

        return ExpenseResult(
            description=description,
            amount=amount,
            category=category
        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""
You are PocketSmart AI,
an expense categorization assistant.

Parse this expense:

{text}

Return:

description
amount
category

Allowed categories:

Food
Transport
Shopping
Entertainment
Bills
Education
Health
Travel
Other

Do not invent an amount.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                ExpenseResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return ExpenseResult.model_validate_json(
        response.text
    )


# ============================================================
# GEMINI RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    month
):

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    spending_data = {

        row["category"]:
            float(row["total"])

        for row in spending

    }

    budget_data = [

        {
            "category":
                row["category"],

            "budget":
                float(row["amount"]),

            "spent":
                spending_data.get(
                    row["category"],
                    0
                )
        }

        for row in budgets

    ]

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        recommendations = []

        for row in budget_data:

            remaining = (
                row["budget"]
                -
                row["spent"]
            )

            if remaining < 0:

                recommendations.append(

                    f"{row['category']} is "
                    f"₹{abs(remaining):,.2f} "
                    "over budget."

                )

            elif (
                row["budget"] > 0
                and
                row["spent"]
                /
                row["budget"]
                >= 0.8
            ):

                recommendations.append(

                    f"{row['category']} has "
                    "used more than 80% "
                    "of its budget."

                )

        if not recommendations:

            recommendations = [

                "Continue recording expenses regularly.",

                "Review your largest spending categories each week.",

                "Set realistic monthly budgets for your main categories."

            ]

        return RecommendationResult(

            summary=(
                f"Total spending for "
                f"{month}: "
                f"₹{total:,.2f}"
            ),

            recommendations=
                recommendations

        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""

You are PocketSmart AI,
a personal budgeting assistant.

Month:
{month}

Total spending:
₹{total:.2f}

Budget information:

{json.dumps(
    budget_data,
    indent=2
)}

Give practical and non-judgmental
budgeting suggestions.

Focus on:

- spending awareness
- category budgets
- expense organization
- everyday budgeting

Do not give investment,
tax, loan, or financial-product advice.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                RecommendationResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return RecommendationResult.model_validate_json(
        response.text
    )


# ============================================================
# API ROUTES
# ============================================================

@app.get("/api/health")
def api_health():

    return jsonify({

        "status": "ok",

        "gemini_configured":
            gemini_client is not None

    })


@app.post("/api/expense")
def api_expense():

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return jsonify({

            "success": False,

            "error":
                "Expense text is required."

        }), 400

    try:

        result = parse_expense_with_ai(
            text
        )

        add_expense(

            result.description,

            result.amount,

            result.category

        )

        return jsonify({

            "success": True,

            "expense":
                result.model_dump()

        })

    except Exception as error:

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


@app.post("/api/manual-expense")
def api_manual_expense():

    data = request.get_json(
        silent=True
    ) or {}

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid amount."

        }), 400

    if amount <= 0:

        return jsonify({

            "success": False,

            "error":
                "Amount must be positive."

        }), 400

    if not description:

        return jsonify({

            "success": False,

            "error":
                "Description is required."

        }), 400

    if category not in CATEGORIES:

        category = "Other"

    add_expense(

        description,

        amount,

        category

    )

    return jsonify({

        "success": True

    })


@app.get("/api/dashboard")
def api_dashboard():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    return jsonify({

        "month":
            month,

        "total":
            total,

        "spending": [

            {

                "category":
                    row["category"],

                "total":
                    float(row["total"])

            }

            for row in spending

        ],

        "budgets": [

            {

                "category":
                    row["category"],

                "amount":
                    float(row["amount"])

            }

            for row in budgets

        ]

    })


@app.post("/api/budget")
def api_budget():

    data = request.get_json(
        silent=True
    ) or {}

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    month = str(
        data.get(
            "month",
            datetime.now().strftime(
                "%Y-%m"
            )
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid budget."

        }), 400

    if category not in CATEGORIES:

        return jsonify({

            "success": False,

            "error":
                "Invalid category."

        }), 400

    if amount < 0:

        return jsonify({

            "success": False,

            "error":
                "Budget cannot be negative."

        }), 400

    save_budget(

        category,

        amount,

        month

    )

    return jsonify({

        "success": True

    })


@app.get("/api/recommendations")
def api_recommendations():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    try:

        result = generate_recommendations(
            month
        )

        return jsonify(
            result.model_dump()
        )

    except Exception as error:

        return jsonify({

            "error": str(error)

        }), 500


# ============================================================
# SINGLE-PAGE FRONTEND
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
    PocketSmart AI
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        #f5f7fb;

    color:
        #172033;

}

header {

    background:
        white;

    border-bottom:
        1px solid #e5e7eb;

    padding:
        18px 6%;

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

}

.logo {

    font-size:
        24px;

    font-weight:
        800;

}

.logo span {

    color:
        #6757d9;

}

nav {

    display:
        flex;

    gap:
        20px;

}

nav a {

    text-decoration:
        none;

    color:
        #555;

}

.container {

    max-width:
        1150px;

    margin:
        auto;

    padding:
        35px 20px;

}

.hero {

    padding:
        40px 0;

}

.hero h1 {

    font-size:
        48px;

    margin:
        10px 0;

}

.hero p {

    color:
        #687184;

    max-width:
        700px;

    line-height:
        1.7;

}

.grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                220px,
                1fr
            )
        );

    gap:
        18px;

}

.card {

    background:
        white;

    border:
        1px solid #e5e7eb;

    border-radius:
        16px;

    padding:
        22px;

    margin-bottom:
        20px;

}

.metric {

    font-size:
        30px;

    font-weight:
        800;

}

.metric small {

    display:
        block;

    color:
        #687184;

    font-size:
        13px;

    font-weight:
        normal;

}

input,
select {

    width:
        100%;

    padding:
        12px;

    margin:
        5px 0;

    border:
        1px solid #d7dce7;

    border-radius:
        8px;

    font-size:
        15px;

}

button {

    padding:
        12px 18px;

    border:
        none;

    border-radius:
        8px;

    background:
        #6757d9;

    color:
        white;

    font-weight:
        700;

    cursor:
        pointer;

    margin-top:
        8px;

}

button:hover {

    background:
        #5546c8;

}

table {

    width:
        100%;

    border-collapse:
        collapse;

}

th,
td {

    padding:
        12px;

    border-bottom:
        1px solid #eee;

    text-align:
        left;

}

.badge {

    background:
        #eeeaff;

    color:
        #6757d9;

    border-radius:
        20px;

    padding:
        4px 9px;

    font-size:
        12px;

}

.ai-result {

    background:
        #f1efff;

    border-radius:
        12px;

    padding:
        15px;

    margin-top:
        15px;

}

.recommendation {

    padding:
        15px;

    border-bottom:
        1px solid #eee;

}

.success {

    color:
        #15733d;

}

.error {

    color:
        #a32626;

}

footer {

    text-align:
        center;

    color:
        #777;

    padding:
        40px;

}

@media(max-width:700px) {

    .hero h1 {

        font-size:
            35px;

    }

    header {

        flex-direction:
            column;

        gap:
            15px;

    }

}

</style>

</head>


<body>


<header>

    <div class="logo">

        PocketSmart
        <span>AI</span>

    </div>


    <nav>

        <a href="#dashboard">
            Dashboard
        </a>

        <a href="#expenses">
            Expenses
        </a>

        <a href="#budgets">
            Budgets
        </a>

        <a href="#ai">
            AI Insights
        </a>

    </nav>

</header>


<div class="container">


<section class="hero">

    <h1>
        Smart Budgeting
        with AI
    </h1>

    <p>

        Track your expenses,
        create budgets,
        categorize spending,
        and get AI-powered
        recommendations.

    </p>

</section>


<section id="dashboard">

<h2>
    Dashboard
</h2>


<div class="grid">


<div class="card">

    <div class="metric">
        ₹<span id="total">0</span>
    </div>

    <small>
        Monthly spending
    </small>

</div>


<div class="card">

    <div class="metric">
        <span id="transactionCount">
            0
        </span>
    </div>

    <small>
        Transactions
    </small>

</div>


<div class="card">

    <div
        class="metric"
        id="aiStatus"
    >
        Checking...
    </div>

    <small>
        AI status
    </small>

</div>


</div>

</section>


<section id="expenses">


<div class="grid">


<div class="card">

<h2>
    Add Expense
</h2>


<form id="manualForm">

<input
    id="description"
    placeholder="Description"
    required
>


<input
    id="amount"
    type="number"
    step="0.01"
    min="0.01"
    placeholder="Amount"
    required
>


<select id="category">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<button>
    Add Expense
</button>

</form>

</div>


<div class="card">

<h2>
    AI Expense Entry
</h2>


<p>
    Example:
    <b>Lunch 180</b>
</p>


<form id="aiForm">

<input
    id="aiText"
    placeholder="Example: Coffee 120"
    required
>


<button>
    Add with AI
</button>

</form>


<div
    id="aiResult"
    class="ai-result"
    style="display:none"
></div>


</div>


</div>


<div class="card">

<h2>
    Recent Expenses
</h2>


<div id="expenseTable">
    Loading...
</div>


</div>


</section>


<section id="budgets">


<div class="card">

<h2>
    Monthly Budget
</h2>


<form id="budgetForm">


<select id="budgetCategory">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<input
    id="budgetAmount"
    type="number"
    step="0.01"
    min="0"
    placeholder="Budget amount"
    required
>


<button>
    Save Budget
</button>


</form>

</div>


</section>


<section id="ai">


<div class="card">

<h2>
    AI Recommendations
</h2>


<button
    onclick="loadRecommendations()"
>
    Generate Recommendations
</button>


<div
    id="recommendations"
    style="margin-top:20px"
></div>


</div>


</section>


</div>


<footer>

PocketSmart AI ·
Python · Flask · SQLite · Gemini

</footer>


<script>

async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            options
        );

    return response.json();

}


// ---------------------------------------------------------
// Load dashboard
// ---------------------------------------------------------

async function loadDashboard() {

    const data =
        await api(
            "/api/dashboard"
        );


    document.getElementById(
        "total"
    ).textContent =
        data.total.toFixed(2);


    const health =
        await api(
            "/api/health"
        );


    document.getElementById(
        "aiStatus"
    ).textContent =
        health.gemini_configured
            ? "Gemini Ready"
            : "Local AI";


    renderExpenses();

}


// ---------------------------------------------------------
// Expenses
// ---------------------------------------------------------

async function renderExpenses() {

    const data =
        await api(
            "/api/dashboard"
        );


    const rows =
        data.spending;


    let html = `

    <table>

        <thead>

            <tr>

                <th>
                    Category
                </th>

                <th>
                    Amount
                </th>

            </tr>

        </thead>

        <tbody>
    `;


    for (
        const row
        of rows
    ) {

        html += `

        <tr>

            <td>

                <span class="badge">
                    ${row.category}
                </span>

            </td>

            <td>
                ₹${row.total.toFixed(2)}
            </td>

        </tr>

        `;

    }


    html += `
        </tbody>
    </table>
    `;


    document.getElementById(
        "expenseTable"
    ).innerHTML =
        html;

}


// ---------------------------------------------------------
// Manual expense
// ---------------------------------------------------------

document.getElementById(
    "manualForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const response =
            await api(
                "/api/manual-expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            description:
                                document
                                .getElementById(
                                    "description"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "amount"
                                )
                                .value,

                            category:
                                document
                                .getElementById(
                                    "category"
                                )
                                .value

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Expense added successfully."
            );

            this.reset();

            loadDashboard();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// AI expense
// ---------------------------------------------------------

document.getElementById(
    "aiForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const text =
            document
            .getElementById(
                "aiText"
            )
            .value;


        const response =
            await api(
                "/api/expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            text: text
                        })

                }
            );


        const result =
            document.getElementById(
                "aiResult"
            );


        result.style.display =
            "block";


        if (
            response.success
        ) {

            result.innerHTML = `

                <b>
                    Expense added
                </b>

                <br><br>

                Description:
                ${response.expense.description}

                <br>

                Amount:
                ₹${response.expense.amount}

                <br>

                Category:
                ${response.expense.category}

            `;


            this.reset();

            loadDashboard();

        } else {

            result.innerHTML = `

                <span class="error">

                    ${response.error}

                </span>

            `;

        }

    }
);


// ---------------------------------------------------------
// Budget
// ---------------------------------------------------------

document.getElementById(
    "budgetForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const now =
            new Date();


        const month =
            now.getFullYear()
            +
            "-"
            +
            String(
                now.getMonth() + 1
            ).padStart(
                2,
                "0"
            );


        const response =
            await api(
                "/api/budget",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            category:
                                document
                                .getElementById(
                                    "budgetCategory"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "budgetAmount"
                                )
                                .value,

                            month:
                                month

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Budget saved."
            );

            this.reset();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// Recommendations
// ---------------------------------------------------------

async function loadRecommendations() {

    const box =
        document.getElementById(
            "recommendations"
        );


    box.innerHTML =
        "Generating AI recommendations...";


    const response =
        await api(
            "/api/recommendations"
        );


    if (
        response.error
    ) {

        box.innerHTML = `

            <p class="error">

                ${response.error}

            </p>

        `;

        return;

    }


    let html = `

        <h3>
            ${response.summary}
        </h3>

    `;


    response.recommendations
        .forEach(
            function(item, index) {

                html += `

                <div class="recommendation">

                    <b>
                        ${index + 1}.
                    </b>

                    ${item}

                </div>

                `;

            }
        );


    box.innerHTML =
        html;

}


// ---------------------------------------------------------
// Start
// ---------------------------------------------------------

loadDashboard();

</script>


</body>

</html>
"""


# ============================================================
# FRONTEND ROUTE
# ============================================================

@app.get("/")
def home():

    return render_template_string(
        HTML
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print(
        "=========================================="
    )

    print(
        "       PocketSmart AI"
    )

    print(
        "=========================================="
    )

    print(
        "Server:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print()

    print(
        "Gemini:",
        "Enabled"
        if gemini_client
        else "Local fallback"
    )

    print()

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True

    )import os
import re
import sqlite3
import json
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

from google import genai
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "pocketsmart-development-secret"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

DATABASE = "pocketsmart.db"


app = Flask(__name__)

app.secret_key = SECRET_KEY


# ============================================================
# GEMINI SETUP
# ============================================================

gemini_client = None

if GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

    except Exception as error:

        print(
            "Gemini initialization failed:",
            error
        )


# ============================================================
# DATA MODELS
# ============================================================

class ExpenseResult(BaseModel):

    description: str = Field(
        description="Short expense description"
    )

    amount: float = Field(
        description="Expense amount"
    )

    category: str = Field(
        description="Expense category"
    )


class RecommendationResult(BaseModel):

    summary: str

    recommendations: list[str]


# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = [

    "Food",

    "Transport",

    "Shopping",

    "Entertainment",

    "Bills",

    "Education",

    "Health",

    "Travel",

    "Other"

]


# ============================================================
# LOCAL CATEGORY FALLBACK
# ============================================================

KEYWORDS = {

    "Food": [
        "food",
        "lunch",
        "dinner",
        "breakfast",
        "coffee",
        "restaurant",
        "pizza",
        "snack",
        "meal",
        "cafe"
    ],

    "Transport": [
        "bus",
        "train",
        "taxi",
        "uber",
        "metro",
        "petrol",
        "fuel",
        "diesel",
        "auto"
    ],

    "Shopping": [
        "shirt",
        "dress",
        "clothes",
        "shoes",
        "shopping",
        "amazon",
        "book",
        "gift"
    ],

    "Entertainment": [
        "movie",
        "cinema",
        "game",
        "music",
        "netflix",
        "concert"
    ],

    "Bills": [
        "bill",
        "electricity",
        "water",
        "internet",
        "phone",
        "rent",
        "recharge"
    ],

    "Education": [
        "school",
        "college",
        "course",
        "tuition",
        "class",
        "exam",
        "education"
    ],

    "Health": [
        "doctor",
        "medicine",
        "pharmacy",
        "hospital",
        "health",
        "medical"
    ],

    "Travel": [
        "travel",
        "hotel",
        "flight",
        "trip",
        "vacation",
        "tour"
    ]

}


def local_category(text):

    text = text.lower()

    for category, words in KEYWORDS.items():

        for word in words:

            if word in text:

                return category

    return "Other"


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


def initialize_database():

    db = get_db()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            description TEXT NOT NULL,

            amount REAL NOT NULL,

            category TEXT NOT NULL,

            created_at TEXT NOT NULL

        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            category TEXT NOT NULL,

            amount REAL NOT NULL,

            month TEXT NOT NULL,

            UNIQUE(category, month)

        )
        """
    )

    db.commit()

    db.close()


initialize_database()


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def add_expense(
    description,
    amount,
    category
):

    db = get_db()

    db.execute(
        """
        INSERT INTO expenses
        (
            description,
            amount,
            category,
            created_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            description,
            amount,
            category,
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    db.commit()

    db.close()


def get_expenses():

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM expenses

        ORDER BY id DESC
        """
    ).fetchall()

    db.close()

    return rows


def get_monthly_total(
    month
):

    db = get_db()

    row = db.execute(
        """
        SELECT
            COALESCE(
                SUM(amount),
                0
            ) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?
        """,
        (month,)
    ).fetchone()

    db.close()

    return float(
        row["total"]
    )


def get_category_spending(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT
            category,
            SUM(amount) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?

        GROUP BY category

        ORDER BY total DESC
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


def save_budget(
    category,
    amount,
    month
):

    db = get_db()

    db.execute(
        """
        INSERT INTO budgets
        (
            category,
            amount,
            month
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            category,
            month
        )

        DO UPDATE SET
            amount = excluded.amount
        """,
        (
            category,
            amount,
            month
        )
    )

    db.commit()

    db.close()


def get_budgets(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM budgets

        WHERE month = ?

        ORDER BY category
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


# ============================================================
# GEMINI EXPENSE PARSER
# ============================================================

def parse_expense_with_ai(
    text
):

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        amount_match = re.search(
            r"(\d+(?:\.\d+)?)",
            text
        )

        if not amount_match:

            raise ValueError(
                "Could not find an amount."
            )

        amount = float(
            amount_match.group(1)
        )

        description = re.sub(
            r"\b\d+(?:\.\d+)?\b",
            "",
            text
        ).strip()

        if not description:

            description = "Expense"

        category = local_category(
            description
        )

        return ExpenseResult(
            description=description,
            amount=amount,
            category=category
        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""
You are PocketSmart AI,
an expense categorization assistant.

Parse this expense:

{text}

Return:

description
amount
category

Allowed categories:

Food
Transport
Shopping
Entertainment
Bills
Education
Health
Travel
Other

Do not invent an amount.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                ExpenseResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return ExpenseResult.model_validate_json(
        response.text
    )


# ============================================================
# GEMINI RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    month
):

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    spending_data = {

        row["category"]:
            float(row["total"])

        for row in spending

    }

    budget_data = [

        {
            "category":
                row["category"],

            "budget":
                float(row["amount"]),

            "spent":
                spending_data.get(
                    row["category"],
                    0
                )
        }

        for row in budgets

    ]

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        recommendations = []

        for row in budget_data:

            remaining = (
                row["budget"]
                -
                row["spent"]
            )

            if remaining < 0:

                recommendations.append(

                    f"{row['category']} is "
                    f"₹{abs(remaining):,.2f} "
                    "over budget."

                )

            elif (
                row["budget"] > 0
                and
                row["spent"]
                /
                row["budget"]
                >= 0.8
            ):

                recommendations.append(

                    f"{row['category']} has "
                    "used more than 80% "
                    "of its budget."

                )

        if not recommendations:

            recommendations = [

                "Continue recording expenses regularly.",

                "Review your largest spending categories each week.",

                "Set realistic monthly budgets for your main categories."

            ]

        return RecommendationResult(

            summary=(
                f"Total spending for "
                f"{month}: "
                f"₹{total:,.2f}"
            ),

            recommendations=
                recommendations

        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""

You are PocketSmart AI,
a personal budgeting assistant.

Month:
{month}

Total spending:
₹{total:.2f}

Budget information:

{json.dumps(
    budget_data,
    indent=2
)}

Give practical and non-judgmental
budgeting suggestions.

Focus on:

- spending awareness
- category budgets
- expense organization
- everyday budgeting

Do not give investment,
tax, loan, or financial-product advice.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                RecommendationResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return RecommendationResult.model_validate_json(
        response.text
    )


# ============================================================
# API ROUTES
# ============================================================

@app.get("/api/health")
def api_health():

    return jsonify({

        "status": "ok",

        "gemini_configured":
            gemini_client is not None

    })


@app.post("/api/expense")
def api_expense():

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return jsonify({

            "success": False,

            "error":
                "Expense text is required."

        }), 400

    try:

        result = parse_expense_with_ai(
            text
        )

        add_expense(

            result.description,

            result.amount,

            result.category

        )

        return jsonify({

            "success": True,

            "expense":
                result.model_dump()

        })

    except Exception as error:

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


@app.post("/api/manual-expense")
def api_manual_expense():

    data = request.get_json(
        silent=True
    ) or {}

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid amount."

        }), 400

    if amount <= 0:

        return jsonify({

            "success": False,

            "error":
                "Amount must be positive."

        }), 400

    if not description:

        return jsonify({

            "success": False,

            "error":
                "Description is required."

        }), 400

    if category not in CATEGORIES:

        category = "Other"

    add_expense(

        description,

        amount,

        category

    )

    return jsonify({

        "success": True

    })


@app.get("/api/dashboard")
def api_dashboard():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    return jsonify({

        "month":
            month,

        "total":
            total,

        "spending": [

            {

                "category":
                    row["category"],

                "total":
                    float(row["total"])

            }

            for row in spending

        ],

        "budgets": [

            {

                "category":
                    row["category"],

                "amount":
                    float(row["amount"])

            }

            for row in budgets

        ]

    })


@app.post("/api/budget")
def api_budget():

    data = request.get_json(
        silent=True
    ) or {}

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    month = str(
        data.get(
            "month",
            datetime.now().strftime(
                "%Y-%m"
            )
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid budget."

        }), 400

    if category not in CATEGORIES:

        return jsonify({

            "success": False,

            "error":
                "Invalid category."

        }), 400

    if amount < 0:

        return jsonify({

            "success": False,

            "error":
                "Budget cannot be negative."

        }), 400

    save_budget(

        category,

        amount,

        month

    )

    return jsonify({

        "success": True

    })


@app.get("/api/recommendations")
def api_recommendations():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    try:

        result = generate_recommendations(
            month
        )

        return jsonify(
            result.model_dump()
        )

    except Exception as error:

        return jsonify({

            "error": str(error)

        }), 500


# ============================================================
# SINGLE-PAGE FRONTEND
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
    PocketSmart AI
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        #f5f7fb;

    color:
        #172033;

}

header {

    background:
        white;

    border-bottom:
        1px solid #e5e7eb;

    padding:
        18px 6%;

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

}

.logo {

    font-size:
        24px;

    font-weight:
        800;

}

.logo span {

    color:
        #6757d9;

}

nav {

    display:
        flex;

    gap:
        20px;

}

nav a {

    text-decoration:
        none;

    color:
        #555;

}

.container {

    max-width:
        1150px;

    margin:
        auto;

    padding:
        35px 20px;

}

.hero {

    padding:
        40px 0;

}

.hero h1 {

    font-size:
        48px;

    margin:
        10px 0;

}

.hero p {

    color:
        #687184;

    max-width:
        700px;

    line-height:
        1.7;

}

.grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                220px,
                1fr
            )
        );

    gap:
        18px;

}

.card {

    background:
        white;

    border:
        1px solid #e5e7eb;

    border-radius:
        16px;

    padding:
        22px;

    margin-bottom:
        20px;

}

.metric {

    font-size:
        30px;

    font-weight:
        800;

}

.metric small {

    display:
        block;

    color:
        #687184;

    font-size:
        13px;

    font-weight:
        normal;

}

input,
select {

    width:
        100%;

    padding:
        12px;

    margin:
        5px 0;

    border:
        1px solid #d7dce7;

    border-radius:
        8px;

    font-size:
        15px;

}

button {

    padding:
        12px 18px;

    border:
        none;

    border-radius:
        8px;

    background:
        #6757d9;

    color:
        white;

    font-weight:
        700;

    cursor:
        pointer;

    margin-top:
        8px;

}

button:hover {

    background:
        #5546c8;

}

table {

    width:
        100%;

    border-collapse:
        collapse;

}

th,
td {

    padding:
        12px;

    border-bottom:
        1px solid #eee;

    text-align:
        left;

}

.badge {

    background:
        #eeeaff;

    color:
        #6757d9;

    border-radius:
        20px;

    padding:
        4px 9px;

    font-size:
        12px;

}

.ai-result {

    background:
        #f1efff;

    border-radius:
        12px;

    padding:
        15px;

    margin-top:
        15px;

}

.recommendation {

    padding:
        15px;

    border-bottom:
        1px solid #eee;

}

.success {

    color:
        #15733d;

}

.error {

    color:
        #a32626;

}

footer {

    text-align:
        center;

    color:
        #777;

    padding:
        40px;

}

@media(max-width:700px) {

    .hero h1 {

        font-size:
            35px;

    }

    header {

        flex-direction:
            column;

        gap:
            15px;

    }

}

</style>

</head>


<body>


<header>

    <div class="logo">

        PocketSmart
        <span>AI</span>

    </div>


    <nav>

        <a href="#dashboard">
            Dashboard
        </a>

        <a href="#expenses">
            Expenses
        </a>

        <a href="#budgets">
            Budgets
        </a>

        <a href="#ai">
            AI Insights
        </a>

    </nav>

</header>


<div class="container">


<section class="hero">

    <h1>
        Smart Budgeting
        with AI
    </h1>

    <p>

        Track your expenses,
        create budgets,
        categorize spending,
        and get AI-powered
        recommendations.

    </p>

</section>


<section id="dashboard">

<h2>
    Dashboard
</h2>


<div class="grid">


<div class="card">

    <div class="metric">
        ₹<span id="total">0</span>
    </div>

    <small>
        Monthly spending
    </small>

</div>


<div class="card">

    <div class="metric">
        <span id="transactionCount">
            0
        </span>
    </div>

    <small>
        Transactions
    </small>

</div>


<div class="card">

    <div
        class="metric"
        id="aiStatus"
    >
        Checking...
    </div>

    <small>
        AI status
    </small>

</div>


</div>

</section>


<section id="expenses">


<div class="grid">


<div class="card">

<h2>
    Add Expense
</h2>


<form id="manualForm">

<input
    id="description"
    placeholder="Description"
    required
>


<input
    id="amount"
    type="number"
    step="0.01"
    min="0.01"
    placeholder="Amount"
    required
>


<select id="category">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<button>
    Add Expense
</button>

</form>

</div>


<div class="card">

<h2>
    AI Expense Entry
</h2>


<p>
    Example:
    <b>Lunch 180</b>
</p>


<form id="aiForm">

<input
    id="aiText"
    placeholder="Example: Coffee 120"
    required
>


<button>
    Add with AI
</button>

</form>


<div
    id="aiResult"
    class="ai-result"
    style="display:none"
></div>


</div>


</div>


<div class="card">

<h2>
    Recent Expenses
</h2>


<div id="expenseTable">
    Loading...
</div>


</div>


</section>


<section id="budgets">


<div class="card">

<h2>
    Monthly Budget
</h2>


<form id="budgetForm">


<select id="budgetCategory">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<input
    id="budgetAmount"
    type="number"
    step="0.01"
    min="0"
    placeholder="Budget amount"
    required
>


<button>
    Save Budget
</button>


</form>

</div>


</section>


<section id="ai">


<div class="card">

<h2>
    AI Recommendations
</h2>


<button
    onclick="loadRecommendations()"
>
    Generate Recommendations
</button>


<div
    id="recommendations"
    style="margin-top:20px"
></div>


</div>


</section>


</div>


<footer>

PocketSmart AI ·
Python · Flask · SQLite · Gemini

</footer>


<script>

async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            options
        );

    return response.json();

}


// ---------------------------------------------------------
// Load dashboard
// ---------------------------------------------------------

async function loadDashboard() {

    const data =
        await api(
            "/api/dashboard"
        );


    document.getElementById(
        "total"
    ).textContent =
        data.total.toFixed(2);


    const health =
        await api(
            "/api/health"
        );


    document.getElementById(
        "aiStatus"
    ).textContent =
        health.gemini_configured
            ? "Gemini Ready"
            : "Local AI";


    renderExpenses();

}


// ---------------------------------------------------------
// Expenses
// ---------------------------------------------------------

async function renderExpenses() {

    const data =
        await api(
            "/api/dashboard"
        );


    const rows =
        data.spending;


    let html = `

    <table>

        <thead>

            <tr>

                <th>
                    Category
                </th>

                <th>
                    Amount
                </th>

            </tr>

        </thead>

        <tbody>
    `;


    for (
        const row
        of rows
    ) {

        html += `

        <tr>

            <td>

                <span class="badge">
                    ${row.category}
                </span>

            </td>

            <td>
                ₹${row.total.toFixed(2)}
            </td>

        </tr>

        `;

    }


    html += `
        </tbody>
    </table>
    `;


    document.getElementById(
        "expenseTable"
    ).innerHTML =
        html;

}


// ---------------------------------------------------------
// Manual expense
// ---------------------------------------------------------

document.getElementById(
    "manualForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const response =
            await api(
                "/api/manual-expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            description:
                                document
                                .getElementById(
                                    "description"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "amount"
                                )
                                .value,

                            category:
                                document
                                .getElementById(
                                    "category"
                                )
                                .value

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Expense added successfully."
            );

            this.reset();

            loadDashboard();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// AI expense
// ---------------------------------------------------------

document.getElementById(
    "aiForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const text =
            document
            .getElementById(
                "aiText"
            )
            .value;


        const response =
            await api(
                "/api/expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            text: text
                        })

                }
            );


        const result =
            document.getElementById(
                "aiResult"
            );


        result.style.display =
            "block";


        if (
            response.success
        ) {

            result.innerHTML = `

                <b>
                    Expense added
                </b>

                <br><br>

                Description:
                ${response.expense.description}

                <br>

                Amount:
                ₹${response.expense.amount}

                <br>

                Category:
                ${response.expense.category}

            `;


            this.reset();

            loadDashboard();

        } else {

            result.innerHTML = `

                <span class="error">

                    ${response.error}

                </span>

            `;

        }

    }
);


// ---------------------------------------------------------
// Budget
// ---------------------------------------------------------

document.getElementById(
    "budgetForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const now =
            new Date();


        const month =
            now.getFullYear()
            +
            "-"
            +
            String(
                now.getMonth() + 1
            ).padStart(
                2,
                "0"
            );


        const response =
            await api(
                "/api/budget",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            category:
                                document
                                .getElementById(
                                    "budgetCategory"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "budgetAmount"
                                )
                                .value,

                            month:
                                month

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Budget saved."
            );

            this.reset();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// Recommendations
// ---------------------------------------------------------

async function loadRecommendations() {

    const box =
        document.getElementById(
            "recommendations"
        );


    box.innerHTML =
        "Generating AI recommendations...";


    const response =
        await api(
            "/api/recommendations"
        );


    if (
        response.error
    ) {

        box.innerHTML = `

            <p class="error">

                ${response.error}

            </p>

        `;

        return;

    }


    let html = `

        <h3>
            ${response.summary}
        </h3>

    `;


    response.recommendations
        .forEach(
            function(item, index) {

                html += `

                <div class="recommendation">

                    <b>
                        ${index + 1}.
                    </b>

                    ${item}

                </div>

                `;

            }
        );


    box.innerHTML =
        html;

}


// ---------------------------------------------------------
// Start
// ---------------------------------------------------------

loadDashboard();

</script>


</body>

</html>
"""


# ============================================================
# FRONTEND ROUTE
# ============================================================

@app.get("/")
def home():

    return render_template_string(
        HTML
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print(
        "=========================================="
    )

    print(
        "       PocketSmart AI"
    )

    print(
        "=========================================="
    )

    print(
        "Server:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print()

    print(
        "Gemini:",
        "Enabled"
        if gemini_client
        else "Local fallback"
    )

    print()

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True

    )import os
import re
import sqlite3
import json
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

from google import genai
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "pocketsmart-development-secret"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

DATABASE = "pocketsmart.db"


app = Flask(__name__)

app.secret_key = SECRET_KEY


# ============================================================
# GEMINI SETUP
# ============================================================

gemini_client = None

if GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

    except Exception as error:

        print(
            "Gemini initialization failed:",
            error
        )


# ============================================================
# DATA MODELS
# ============================================================

class ExpenseResult(BaseModel):

    description: str = Field(
        description="Short expense description"
    )

    amount: float = Field(
        description="Expense amount"
    )

    category: str = Field(
        description="Expense category"
    )


class RecommendationResult(BaseModel):

    summary: str

    recommendations: list[str]


# ============================================================
# CATEGORIES
# ============================================================

CATEGORIES = [

    "Food",

    "Transport",

    "Shopping",

    "Entertainment",

    "Bills",

    "Education",

    "Health",

    "Travel",

    "Other"

]


# ============================================================
# LOCAL CATEGORY FALLBACK
# ============================================================

KEYWORDS = {

    "Food": [
        "food",
        "lunch",
        "dinner",
        "breakfast",
        "coffee",
        "restaurant",
        "pizza",
        "snack",
        "meal",
        "cafe"
    ],

    "Transport": [
        "bus",
        "train",
        "taxi",
        "uber",
        "metro",
        "petrol",
        "fuel",
        "diesel",
        "auto"
    ],

    "Shopping": [
        "shirt",
        "dress",
        "clothes",
        "shoes",
        "shopping",
        "amazon",
        "book",
        "gift"
    ],

    "Entertainment": [
        "movie",
        "cinema",
        "game",
        "music",
        "netflix",
        "concert"
    ],

    "Bills": [
        "bill",
        "electricity",
        "water",
        "internet",
        "phone",
        "rent",
        "recharge"
    ],

    "Education": [
        "school",
        "college",
        "course",
        "tuition",
        "class",
        "exam",
        "education"
    ],

    "Health": [
        "doctor",
        "medicine",
        "pharmacy",
        "hospital",
        "health",
        "medical"
    ],

    "Travel": [
        "travel",
        "hotel",
        "flight",
        "trip",
        "vacation",
        "tour"
    ]

}


def local_category(text):

    text = text.lower()

    for category, words in KEYWORDS.items():

        for word in words:

            if word in text:

                return category

    return "Other"


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


def initialize_database():

    db = get_db()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            description TEXT NOT NULL,

            amount REAL NOT NULL,

            category TEXT NOT NULL,

            created_at TEXT NOT NULL

        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            category TEXT NOT NULL,

            amount REAL NOT NULL,

            month TEXT NOT NULL,

            UNIQUE(category, month)

        )
        """
    )

    db.commit()

    db.close()


initialize_database()


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def add_expense(
    description,
    amount,
    category
):

    db = get_db()

    db.execute(
        """
        INSERT INTO expenses
        (
            description,
            amount,
            category,
            created_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            description,
            amount,
            category,
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    db.commit()

    db.close()


def get_expenses():

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM expenses

        ORDER BY id DESC
        """
    ).fetchall()

    db.close()

    return rows


def get_monthly_total(
    month
):

    db = get_db()

    row = db.execute(
        """
        SELECT
            COALESCE(
                SUM(amount),
                0
            ) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?
        """,
        (month,)
    ).fetchone()

    db.close()

    return float(
        row["total"]
    )


def get_category_spending(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT
            category,
            SUM(amount) AS total

        FROM expenses

        WHERE substr(
            created_at,
            1,
            7
        ) = ?

        GROUP BY category

        ORDER BY total DESC
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


def save_budget(
    category,
    amount,
    month
):

    db = get_db()

    db.execute(
        """
        INSERT INTO budgets
        (
            category,
            amount,
            month
        )

        VALUES (?, ?, ?)

        ON CONFLICT(
            category,
            month
        )

        DO UPDATE SET
            amount = excluded.amount
        """,
        (
            category,
            amount,
            month
        )
    )

    db.commit()

    db.close()


def get_budgets(
    month
):

    db = get_db()

    rows = db.execute(
        """
        SELECT *

        FROM budgets

        WHERE month = ?

        ORDER BY category
        """,
        (month,)
    ).fetchall()

    db.close()

    return rows


# ============================================================
# GEMINI EXPENSE PARSER
# ============================================================

def parse_expense_with_ai(
    text
):

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        amount_match = re.search(
            r"(\d+(?:\.\d+)?)",
            text
        )

        if not amount_match:

            raise ValueError(
                "Could not find an amount."
            )

        amount = float(
            amount_match.group(1)
        )

        description = re.sub(
            r"\b\d+(?:\.\d+)?\b",
            "",
            text
        ).strip()

        if not description:

            description = "Expense"

        category = local_category(
            description
        )

        return ExpenseResult(
            description=description,
            amount=amount,
            category=category
        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""
You are PocketSmart AI,
an expense categorization assistant.

Parse this expense:

{text}

Return:

description
amount
category

Allowed categories:

Food
Transport
Shopping
Entertainment
Bills
Education
Health
Travel
Other

Do not invent an amount.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                ExpenseResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return ExpenseResult.model_validate_json(
        response.text
    )


# ============================================================
# GEMINI RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    month
):

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    spending_data = {

        row["category"]:
            float(row["total"])

        for row in spending

    }

    budget_data = [

        {
            "category":
                row["category"],

            "budget":
                float(row["amount"]),

            "spent":
                spending_data.get(
                    row["category"],
                    0
                )
        }

        for row in budgets

    ]

    # -----------------------------------------
    # Local fallback
    # -----------------------------------------

    if not gemini_client:

        recommendations = []

        for row in budget_data:

            remaining = (
                row["budget"]
                -
                row["spent"]
            )

            if remaining < 0:

                recommendations.append(

                    f"{row['category']} is "
                    f"₹{abs(remaining):,.2f} "
                    "over budget."

                )

            elif (
                row["budget"] > 0
                and
                row["spent"]
                /
                row["budget"]
                >= 0.8
            ):

                recommendations.append(

                    f"{row['category']} has "
                    "used more than 80% "
                    "of its budget."

                )

        if not recommendations:

            recommendations = [

                "Continue recording expenses regularly.",

                "Review your largest spending categories each week.",

                "Set realistic monthly budgets for your main categories."

            ]

        return RecommendationResult(

            summary=(
                f"Total spending for "
                f"{month}: "
                f"₹{total:,.2f}"
            ),

            recommendations=
                recommendations

        )

    # -----------------------------------------
    # Gemini
    # -----------------------------------------

    prompt = f"""

You are PocketSmart AI,
a personal budgeting assistant.

Month:
{month}

Total spending:
₹{total:.2f}

Budget information:

{json.dumps(
    budget_data,
    indent=2
)}

Give practical and non-judgmental
budgeting suggestions.

Focus on:

- spending awareness
- category budgets
- expense organization
- everyday budgeting

Do not give investment,
tax, loan, or financial-product advice.

Return structured JSON.
"""

    response = gemini_client.models.generate_content(

        model=GEMINI_MODEL,

        contents=prompt,

        config={

            "response_mime_type":
                "application/json",

            "response_schema":
                RecommendationResult

        }

    )

    if getattr(
        response,
        "parsed",
        None
    ):

        return response.parsed

    return RecommendationResult.model_validate_json(
        response.text
    )


# ============================================================
# API ROUTES
# ============================================================

@app.get("/api/health")
def api_health():

    return jsonify({

        "status": "ok",

        "gemini_configured":
            gemini_client is not None

    })


@app.post("/api/expense")
def api_expense():

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return jsonify({

            "success": False,

            "error":
                "Expense text is required."

        }), 400

    try:

        result = parse_expense_with_ai(
            text
        )

        add_expense(

            result.description,

            result.amount,

            result.category

        )

        return jsonify({

            "success": True,

            "expense":
                result.model_dump()

        })

    except Exception as error:

        return jsonify({

            "success": False,

            "error": str(error)

        }), 500


@app.post("/api/manual-expense")
def api_manual_expense():

    data = request.get_json(
        silent=True
    ) or {}

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid amount."

        }), 400

    if amount <= 0:

        return jsonify({

            "success": False,

            "error":
                "Amount must be positive."

        }), 400

    if not description:

        return jsonify({

            "success": False,

            "error":
                "Description is required."

        }), 400

    if category not in CATEGORIES:

        category = "Other"

    add_expense(

        description,

        amount,

        category

    )

    return jsonify({

        "success": True

    })


@app.get("/api/dashboard")
def api_dashboard():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    total = get_monthly_total(
        month
    )

    spending = get_category_spending(
        month
    )

    budgets = get_budgets(
        month
    )

    return jsonify({

        "month":
            month,

        "total":
            total,

        "spending": [

            {

                "category":
                    row["category"],

                "total":
                    float(row["total"])

            }

            for row in spending

        ],

        "budgets": [

            {

                "category":
                    row["category"],

                "amount":
                    float(row["amount"])

            }

            for row in budgets

        ]

    })


@app.post("/api/budget")
def api_budget():

    data = request.get_json(
        silent=True
    ) or {}

    category = str(
        data.get(
            "category",
            "Other"
        )
    )

    month = str(
        data.get(
            "month",
            datetime.now().strftime(
                "%Y-%m"
            )
        )
    )

    try:

        amount = float(
            data.get(
                "amount"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({

            "success": False,

            "error":
                "Invalid budget."

        }), 400

    if category not in CATEGORIES:

        return jsonify({

            "success": False,

            "error":
                "Invalid category."

        }), 400

    if amount < 0:

        return jsonify({

            "success": False,

            "error":
                "Budget cannot be negative."

        }), 400

    save_budget(

        category,

        amount,

        month

    )

    return jsonify({

        "success": True

    })


@app.get("/api/recommendations")
def api_recommendations():

    month = request.args.get(

        "month",

        datetime.now().strftime(
            "%Y-%m"
        )

    )

    try:

        result = generate_recommendations(
            month
        )

        return jsonify(
            result.model_dump()
        )

    except Exception as error:

        return jsonify({

            "error": str(error)

        }), 500


# ============================================================
# SINGLE-PAGE FRONTEND
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
    PocketSmart AI
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        #f5f7fb;

    color:
        #172033;

}

header {

    background:
        white;

    border-bottom:
        1px solid #e5e7eb;

    padding:
        18px 6%;

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

}

.logo {

    font-size:
        24px;

    font-weight:
        800;

}

.logo span {

    color:
        #6757d9;

}

nav {

    display:
        flex;

    gap:
        20px;

}

nav a {

    text-decoration:
        none;

    color:
        #555;

}

.container {

    max-width:
        1150px;

    margin:
        auto;

    padding:
        35px 20px;

}

.hero {

    padding:
        40px 0;

}

.hero h1 {

    font-size:
        48px;

    margin:
        10px 0;

}

.hero p {

    color:
        #687184;

    max-width:
        700px;

    line-height:
        1.7;

}

.grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                220px,
                1fr
            )
        );

    gap:
        18px;

}

.card {

    background:
        white;

    border:
        1px solid #e5e7eb;

    border-radius:
        16px;

    padding:
        22px;

    margin-bottom:
        20px;

}

.metric {

    font-size:
        30px;

    font-weight:
        800;

}

.metric small {

    display:
        block;

    color:
        #687184;

    font-size:
        13px;

    font-weight:
        normal;

}

input,
select {

    width:
        100%;

    padding:
        12px;

    margin:
        5px 0;

    border:
        1px solid #d7dce7;

    border-radius:
        8px;

    font-size:
        15px;

}

button {

    padding:
        12px 18px;

    border:
        none;

    border-radius:
        8px;

    background:
        #6757d9;

    color:
        white;

    font-weight:
        700;

    cursor:
        pointer;

    margin-top:
        8px;

}

button:hover {

    background:
        #5546c8;

}

table {

    width:
        100%;

    border-collapse:
        collapse;

}

th,
td {

    padding:
        12px;

    border-bottom:
        1px solid #eee;

    text-align:
        left;

}

.badge {

    background:
        #eeeaff;

    color:
        #6757d9;

    border-radius:
        20px;

    padding:
        4px 9px;

    font-size:
        12px;

}

.ai-result {

    background:
        #f1efff;

    border-radius:
        12px;

    padding:
        15px;

    margin-top:
        15px;

}

.recommendation {

    padding:
        15px;

    border-bottom:
        1px solid #eee;

}

.success {

    color:
        #15733d;

}

.error {

    color:
        #a32626;

}

footer {

    text-align:
        center;

    color:
        #777;

    padding:
        40px;

}

@media(max-width:700px) {

    .hero h1 {

        font-size:
            35px;

    }

    header {

        flex-direction:
            column;

        gap:
            15px;

    }

}

</style>

</head>


<body>


<header>

    <div class="logo">

        PocketSmart
        <span>AI</span>

    </div>


    <nav>

        <a href="#dashboard">
            Dashboard
        </a>

        <a href="#expenses">
            Expenses
        </a>

        <a href="#budgets">
            Budgets
        </a>

        <a href="#ai">
            AI Insights
        </a>

    </nav>

</header>


<div class="container">


<section class="hero">

    <h1>
        Smart Budgeting
        with AI
    </h1>

    <p>

        Track your expenses,
        create budgets,
        categorize spending,
        and get AI-powered
        recommendations.

    </p>

</section>


<section id="dashboard">

<h2>
    Dashboard
</h2>


<div class="grid">


<div class="card">

    <div class="metric">
        ₹<span id="total">0</span>
    </div>

    <small>
        Monthly spending
    </small>

</div>


<div class="card">

    <div class="metric">
        <span id="transactionCount">
            0
        </span>
    </div>

    <small>
        Transactions
    </small>

</div>


<div class="card">

    <div
        class="metric"
        id="aiStatus"
    >
        Checking...
    </div>

    <small>
        AI status
    </small>

</div>


</div>

</section>


<section id="expenses">


<div class="grid">


<div class="card">

<h2>
    Add Expense
</h2>


<form id="manualForm">

<input
    id="description"
    placeholder="Description"
    required
>


<input
    id="amount"
    type="number"
    step="0.01"
    min="0.01"
    placeholder="Amount"
    required
>


<select id="category">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<button>
    Add Expense
</button>

</form>

</div>


<div class="card">

<h2>
    AI Expense Entry
</h2>


<p>
    Example:
    <b>Lunch 180</b>
</p>


<form id="aiForm">

<input
    id="aiText"
    placeholder="Example: Coffee 120"
    required
>


<button>
    Add with AI
</button>

</form>


<div
    id="aiResult"
    class="ai-result"
    style="display:none"
></div>


</div>


</div>


<div class="card">

<h2>
    Recent Expenses
</h2>


<div id="expenseTable">
    Loading...
</div>


</div>


</section>


<section id="budgets">


<div class="card">

<h2>
    Monthly Budget
</h2>


<form id="budgetForm">


<select id="budgetCategory">

<option>Food</option>
<option>Transport</option>
<option>Shopping</option>
<option>Entertainment</option>
<option>Bills</option>
<option>Education</option>
<option>Health</option>
<option>Travel</option>
<option>Other</option>

</select>


<input
    id="budgetAmount"
    type="number"
    step="0.01"
    min="0"
    placeholder="Budget amount"
    required
>


<button>
    Save Budget
</button>


</form>

</div>


</section>


<section id="ai">


<div class="card">

<h2>
    AI Recommendations
</h2>


<button
    onclick="loadRecommendations()"
>
    Generate Recommendations
</button>


<div
    id="recommendations"
    style="margin-top:20px"
></div>


</div>


</section>


</div>


<footer>

PocketSmart AI ·
Python · Flask · SQLite · Gemini

</footer>


<script>

async function api(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            options
        );

    return response.json();

}


// ---------------------------------------------------------
// Load dashboard
// ---------------------------------------------------------

async function loadDashboard() {

    const data =
        await api(
            "/api/dashboard"
        );


    document.getElementById(
        "total"
    ).textContent =
        data.total.toFixed(2);


    const health =
        await api(
            "/api/health"
        );


    document.getElementById(
        "aiStatus"
    ).textContent =
        health.gemini_configured
            ? "Gemini Ready"
            : "Local AI";


    renderExpenses();

}


// ---------------------------------------------------------
// Expenses
// ---------------------------------------------------------

async function renderExpenses() {

    const data =
        await api(
            "/api/dashboard"
        );


    const rows =
        data.spending;


    let html = `

    <table>

        <thead>

            <tr>

                <th>
                    Category
                </th>

                <th>
                    Amount
                </th>

            </tr>

        </thead>

        <tbody>
    `;


    for (
        const row
        of rows
    ) {

        html += `

        <tr>

            <td>

                <span class="badge">
                    ${row.category}
                </span>

            </td>

            <td>
                ₹${row.total.toFixed(2)}
            </td>

        </tr>

        `;

    }


    html += `
        </tbody>
    </table>
    `;


    document.getElementById(
        "expenseTable"
    ).innerHTML =
        html;

}


// ---------------------------------------------------------
// Manual expense
// ---------------------------------------------------------

document.getElementById(
    "manualForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const response =
            await api(
                "/api/manual-expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            description:
                                document
                                .getElementById(
                                    "description"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "amount"
                                )
                                .value,

                            category:
                                document
                                .getElementById(
                                    "category"
                                )
                                .value

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Expense added successfully."
            );

            this.reset();

            loadDashboard();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// AI expense
// ---------------------------------------------------------

document.getElementById(
    "aiForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const text =
            document
            .getElementById(
                "aiText"
            )
            .value;


        const response =
            await api(
                "/api/expense",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            text: text
                        })

                }
            );


        const result =
            document.getElementById(
                "aiResult"
            );


        result.style.display =
            "block";


        if (
            response.success
        ) {

            result.innerHTML = `

                <b>
                    Expense added
                </b>

                <br><br>

                Description:
                ${response.expense.description}

                <br>

                Amount:
                ₹${response.expense.amount}

                <br>

                Category:
                ${response.expense.category}

            `;


            this.reset();

            loadDashboard();

        } else {

            result.innerHTML = `

                <span class="error">

                    ${response.error}

                </span>

            `;

        }

    }
);


// ---------------------------------------------------------
// Budget
// ---------------------------------------------------------

document.getElementById(
    "budgetForm"
).addEventListener(
    "submit",
    async function(event) {

        event.preventDefault();


        const now =
            new Date();


        const month =
            now.getFullYear()
            +
            "-"
            +
            String(
                now.getMonth() + 1
            ).padStart(
                2,
                "0"
            );


        const response =
            await api(
                "/api/budget",
                {

                    method:
                        "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({

                            category:
                                document
                                .getElementById(
                                    "budgetCategory"
                                )
                                .value,

                            amount:
                                document
                                .getElementById(
                                    "budgetAmount"
                                )
                                .value,

                            month:
                                month

                        })

                }
            );


        if (
            response.success
        ) {

            alert(
                "Budget saved."
            );

            this.reset();

        } else {

            alert(
                response.error
            );

        }

    }
);


// ---------------------------------------------------------
// Recommendations
// ---------------------------------------------------------

async function loadRecommendations() {

    const box =
        document.getElementById(
            "recommendations"
        );


    box.innerHTML =
        "Generating AI recommendations...";


    const response =
        await api(
            "/api/recommendations"
        );


    if (
        response.error
    ) {

        box.innerHTML = `

            <p class="error">

                ${response.error}

            </p>

        `;

        return;

    }


    let html = `

        <h3>
            ${response.summary}
        </h3>

    `;


    response.recommendations
        .forEach(
            function(item, index) {

                html += `

                <div class="recommendation">

                    <b>
                        ${index + 1}.
                    </b>

                    ${item}

                </div>

                `;

            }
        );


    box.innerHTML =
        html;

}


// ---------------------------------------------------------
// Start
// ---------------------------------------------------------

loadDashboard();

</script>


</body>

</html>
"""


# ============================================================
# FRONTEND ROUTE
# ============================================================

@app.get("/")
def home():

    return render_template_string(
        HTML
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print(
        "=========================================="
    )

    print(
        "       PocketSmart AI"
    )

    print(
        "=========================================="
    )

    print(
        "Server:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print()

    print(
        "Gemini:",
        "Enabled"
        if gemini_client
        else "Local fallback"
    )

    print()

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True

    )