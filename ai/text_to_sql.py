import os
import json

import pandas as pd
import streamlit as st
import snowflake.connector
from google import genai
from dotenv import load_dotenv


# ---------------------------------------------------------
# Environment
# ---------------------------------------------------------

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")


# ---------------------------------------------------------
# Gemini client
# ---------------------------------------------------------

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)


# ---------------------------------------------------------
# Example questions
# ---------------------------------------------------------

EXAMPLE_QUESTIONS = [
    "Top 10 cities by GMV",
    "Which cuisine has the most orders?",
    "Average delivery time by city, worst first",
    "Cancel rate by payment method",
]


# ---------------------------------------------------------
# Snowflake schema exposed to Gemini
# ---------------------------------------------------------

SCHEMA = """
Available Snowflake tables.

Use ONLY these tables and columns.

FCT_ORDERS(
    order_id,
    order_date,
    customer_id,
    restaurant_id,
    city,
    cuisine,
    payment_method,
    order_status,
    is_delivered,
    sales_amount,
    discount,
    delivery_fee,
    gst,
    customer_rating,
    delivery_time_min
)

DIM_RESTAURANT(
    restaurant_id,
    restaurant_name,
    city,
    cuisine,
    rating,
    cost_for_two
)

DIM_CUSTOMER(
    customer_id,
    customer_name,
    age,
    age_segment,
    gender,
    city
)

MART_DAILY_CITY_REVENUE(
    order_date,
    city,
    orders,
    cancel_rate,
    gmv,
    aov
)

MART_RESTAURANT_PERFORMANCE(
    restaurant_id,
    restaurant_name,
    city,
    cuisine,
    orders,
    revenue,
    avg_customer_rating,
    cancel_rate
)

MART_DELIVERY_SLA(
    city,
    order_hour,
    delivered_orders,
    p50_delivery_min,
    late_rate
)

Important:
- gmv means delivered revenue.
- Prefer MART_ tables when they directly answer the question.
- Use FCT_ORDERS when detailed order-level analysis is required.
- Use DIM_CUSTOMER and DIM_RESTAURANT when customer or restaurant attributes are needed.
"""


# ---------------------------------------------------------
# Gemini system prompt
# ---------------------------------------------------------

SYSTEM_PROMPT = f"""
You are a Snowflake SQL expert.

Your task is to convert the user's natural-language
business question into ONE valid Snowflake SELECT query.

Rules:

1. Generate ONLY SELECT or WITH ... SELECT queries.
2. Never modify, insert, delete, update, create, alter,
   drop, truncate, merge, grant, or revoke anything.
3. Use ONLY the tables and columns listed in the schema.
4. Use bare table names.
   Example:
       FCT_ORDERS

   Do NOT use:
       ZOMATO.MARTS.FCT_ORDERS
5. Use Snowflake-compatible SQL.
6. Add LIMIT 100 or less for detailed result sets.
7. For a single aggregate result, a LIMIT is not necessary.
8. Use meaningful aliases for calculated columns.
9. If the question asks for "top", sort descending.
10. If the question asks for "worst first", sort descending
    by the relevant metric.
11. Do not invent columns or tables.
12. Return ONLY valid JSON.

Required JSON format:

{{
    "sql": "SELECT ..."
}}

Database schema:

{SCHEMA}
"""


# ---------------------------------------------------------
# Snowflake connection
# ---------------------------------------------------------

@st.cache_resource
def get_connection():
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE", "ZOMATO"),
        schema="MARTS",
        role="DBT_ROLE",
    )


# ---------------------------------------------------------
# Generate SQL using Gemini
# ---------------------------------------------------------

def generate_sql(question):
    response = client.models.generate_content(
        model=MODEL,
        contents=[
            SYSTEM_PROMPT,
            f"User question:\n{question}",
        ],
        config={
            "temperature": 0,
            "response_mime_type": "application/json",
        },
    )

    if not response.text:
        raise ValueError("Gemini returned an empty response.")

    try:
        result = json.loads(response.text)
    except json.JSONDecodeError:
        raise ValueError(
            f"Gemini did not return valid JSON:\n{response.text}"
        )

    sql = result.get("sql")

    if not sql:
        raise ValueError("Gemini response does not contain SQL.")

    # Remove accidental database/schema prefixes.
    sql = sql.replace("ZOMATO.MARTS.", "")
    sql = sql.replace("ZOMATO.", "")

    return sql.strip().rstrip(";")


# ---------------------------------------------------------
# SQL safety check
# ---------------------------------------------------------

FORBIDDEN_PATTERNS = [
    "drop ",
    "delete ",
    "truncate ",
    "alter ",
    "update ",
    "insert ",
    "create ",
    "replace ",
    "grant ",
    "revoke ",
    "merge ",
]


def is_safe(sql):
    cleaned_sql = sql.strip().lower()

    # Only SELECT or WITH queries are allowed.
    if not (
        cleaned_sql.startswith("select ")
        or cleaned_sql.startswith("with ")
    ):
        return False

    # Prevent multiple statements.
    if ";" in cleaned_sql:
        return False

    # Block modification statements.
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in cleaned_sql:
            return False

    return True


# ---------------------------------------------------------
# Execute SQL
# ---------------------------------------------------------

def run_query(sql):
    conn = get_connection()

    cursor = conn.cursor()

    try:
        dataframe = cursor.execute(sql).fetch_pandas_all()
        return dataframe

    finally:
        cursor.close()


# ---------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------

st.set_page_config(
    page_title="Zomato AI Analytics",
    page_icon="🍽️",
    layout="wide",
)

st.title("🍽️ Chat with your Zomato Data")

st.caption(
    f"Ask a business question in English → "
    f"{MODEL} generates Snowflake SQL → "
    f"Snowflake executes it"
)


# ---------------------------------------------------------
# Sidebar
# ---------------------------------------------------------

with st.sidebar:
    st.header("Example Questions")

    for question_example in EXAMPLE_QUESTIONS:
        st.markdown(f"- {question_example}")


# ---------------------------------------------------------
# User question
# ---------------------------------------------------------

question = st.text_input(
    "Enter your question",
    placeholder=(
        "e.g. Top 10 restaurants by revenue in Bangalore"
    ),
)


# ---------------------------------------------------------
# Process question
# ---------------------------------------------------------

if question:

    try:

        # ---------------------------------------------
        # Step 1: Generate SQL
        # ---------------------------------------------

        with st.spinner("Generating SQL..."):
            sql = generate_sql(question)

        st.subheader("Generated SQL")

        st.code(
            sql,
            language="sql",
        )


        # ---------------------------------------------
        # Step 2: Safety check
        # ---------------------------------------------

        if not is_safe(sql):

            st.error(
                "The generated SQL was blocked because "
                "it is not a safe read-only SELECT query."
            )

            st.stop()


        # ---------------------------------------------
        # Step 3: Execute SQL
        # ---------------------------------------------

        with st.spinner("Running query in Snowflake..."):
            dataframe = run_query(sql)


        # ---------------------------------------------
        # Step 4: Display result
        # ---------------------------------------------

        st.success(
            f"{len(dataframe)} rows returned"
        )

        st.dataframe(
            dataframe,
            hide_index=True,
            use_container_width=True,
        )


        # ---------------------------------------------
        # Step 5: Simple chart
        # ---------------------------------------------

        if (
            len(dataframe.columns) == 2
            and len(dataframe) > 0
            and pd.api.types.is_numeric_dtype(
                dataframe.iloc[:, 1]
            )
        ):

            st.subheader("Visualization")

            st.bar_chart(
                dataframe,
                x=dataframe.columns[0],
                y=dataframe.columns[1],
            )


    except Exception as e:

        st.error(
            f"Something went wrong: {e}"
        )