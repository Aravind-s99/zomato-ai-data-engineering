import os
import time
import numpy as np
import pandas as pd
import streamlit as st
import snowflake.connector
from google import genai
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

EMBEDDING_MODEL = "gemini-embedding-001"
CHAT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

NEW_REVIEWS = 50
TOP_K = 5

CACHE_FILE = "review_embeddings.parquet"

BATCH_SIZE = 50
MAX_RETRIES = 4


# ---------------------------------------------------------
# Gemini client
# ---------------------------------------------------------

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)


# ---------------------------------------------------------
# Snowflake
# ---------------------------------------------------------

def read_reviews_from_snowflake():

    conn = snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA"),
    )

    query = f"""
        SELECT REVIEW_ID, CITY, RATING, COMMENT
        FROM ZOMATO.STAGING.STG_REVIEWS
        SAMPLE ({NEW_REVIEWS} ROWS)
    """

    cursor = conn.cursor()

    try:
        df = cursor.execute(query).fetch_pandas_all()
    finally:
        cursor.close()
        conn.close()

    df.columns = [col.lower() for col in df.columns]

    return df


# ---------------------------------------------------------
# Gemini Embeddings
# ---------------------------------------------------------

def embed(texts):
    all_embeddings = []
    total = len(texts)

    for i in range(0, total, BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]

        print(
            f"Generating embeddings "
            f"{i + 1}-{min(i + BATCH_SIZE, total)} "
            f"of {total}..."
        )

        for attempt in range(MAX_RETRIES):
            try:
                response = client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch
                )

                all_embeddings.extend(
                    embedding.values
                    for embedding in response.embeddings
                )

                # Give the free-tier quota some breathing room
                if i + BATCH_SIZE < total:
                    print("Waiting 15 seconds before next batch...")
                    time.sleep(15)

                break

            except Exception as e:
                error_message = str(e)

                if (
                    "429" in error_message
                    or "RESOURCE_EXHAUSTED" in error_message
                ):
                    if attempt == MAX_RETRIES - 1:
                        raise

                    # Gemini tells us how long to wait.
                    # Your current error says 45 seconds.
                    wait_time = 50

                    print(
                        f"Embedding quota reached. "
                        f"Waiting {wait_time} seconds before retry..."
                    )

                    time.sleep(wait_time)

                else:
                    raise

    return all_embeddings


# ---------------------------------------------------------
# Load reviews + cache embeddings
# ---------------------------------------------------------

@st.cache_data(show_spinner=False)
def load_reviews():

    # Use existing local cache if available
    if os.path.exists(CACHE_FILE):

        print("Loading review embeddings from cache...")

        return pd.read_parquet(CACHE_FILE)

    print("No embedding cache found.")

    print("Loading reviews from Snowflake...")

    df = read_reviews_from_snowflake()

    if df.empty:
        raise ValueError(
            "No reviews were returned from Snowflake."
        )

    # Remove null/empty comments
    df["comment"] = (
        df["comment"]
        .fillna("")
        .astype(str)
    )

    df = df[
        df["comment"].str.strip() != ""
    ].reset_index(drop=True)

    print(
        f"Generating embeddings for "
        f"{len(df)} reviews..."
    )

    df["embedding"] = embed(
        df["comment"].tolist()
    )

    # Save embeddings locally
    df.to_parquet(
        CACHE_FILE,
        index=False
    )

    print(
        f"Embeddings saved to {CACHE_FILE}"
    )

    return df


# ---------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------

st.title("Chat with your Zomato Reviews")

st.caption(
    f"Searching {NEW_REVIEWS} reviews, "
    f"answering with {CHAT_MODEL}"
)


# ---------------------------------------------------------
# Cosine Similarity
# ---------------------------------------------------------

def cosine_similarity(vec_a, vec_b):

    denominator = (
        np.linalg.norm(vec_a)
        * np.linalg.norm(vec_b)
    )

    if denominator == 0:
        return 0.0

    return np.dot(vec_a, vec_b) / denominator


# ---------------------------------------------------------
# Semantic Search
# ---------------------------------------------------------

def find_similar_reviews(question, df):

    question_vector = embed([question])[0]

    scores = []

    for review_vector in df["embedding"]:

        scores.append(
            cosine_similarity(
                question_vector,
                review_vector
            )
        )

    result = df.copy()

    result["score"] = scores

    return result.nlargest(
        TOP_K,
        "score"
    )


# ---------------------------------------------------------
# Gemini Answer
# ---------------------------------------------------------

def ask_llm(question, top_reviews):

    context = ""

    for _, row in top_reviews.iterrows():

        context += (
            f"City: {row['city']}\n"
            f"Rating: {row['rating']} stars\n"
            f"Review: {row['comment']}\n\n"
        )

    system_prompt = """
You answer questions about Zomato customer reviews.

Use ONLY the customer reviews provided.

Do not invent information.

Be concise and directly answer the question.

If the provided reviews do not contain enough information
to answer the question, say:
"I don't have enough information in the retrieved reviews."
"""

    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Retrieved customer reviews:\n{context}"
    )

    for attempt in range(MAX_RETRIES):

        try:

            response = client.models.generate_content(
                model=CHAT_MODEL,
                contents=[
                    system_prompt,
                    user_prompt
                ]
            )

            return response.text

        except Exception as e:

            error_message = str(e)

            if (
                "429" in error_message
                or "RESOURCE_EXHAUSTED" in error_message
                or "503" in error_message
                or "UNAVAILABLE" in error_message
            ):

                if attempt == MAX_RETRIES - 1:
                    raise

                wait_time = 2 ** attempt + 2

                print(
                    f"Gemini temporarily unavailable. "
                    f"Retrying in {wait_time} seconds..."
                )

                time.sleep(wait_time)

            else:
                raise


# ---------------------------------------------------------
# Main Application
# ---------------------------------------------------------

try:

    review_df = load_reviews()

    question = st.text_input(
        "Ask a question about your reviews:",
        placeholder=(
            "e.g. What are the most common complaints "
            "about delivery?"
        )
    )

    if question:

        with st.spinner("Searching reviews..."):

            top_reviews = find_similar_reviews(
                question,
                review_df
            )

        with st.spinner("Generating answer..."):

            answer = ask_llm(
                question,
                top_reviews
            )

        st.markdown("**Answer:**")

        st.write(answer)

        with st.expander(
            "Reviews used to build this answer"
        ):

            st.dataframe(
                top_reviews[
                    [
                        "city",
                        "rating",
                        "comment"
                    ]
                ],
                hide_index=True
            )

except Exception as e:

    st.error(
        f"Something went wrong: {e}"
    )