import streamlit as st

st.title("Minimal Streamlit App")

st.write("Hello, Streamlit! This is a minimal example.")

x = st.slider("Pick a number", 0, 10, 1)
st.write(f"Square of {x} is {x ** 2}")
