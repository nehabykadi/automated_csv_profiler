# Automated CSV Profiler

Python 3.13.7

Required libraries: Numpy, Pandas, Matplotlib

### Installation and Run Steps
```
# If libraries are not installed
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# To run script
python src/profiler.py data/your_file.csv            # (needs Ollama)
python src/profiler.py data/your_file.csv --no-llm
```
1. Select a CSV through a path (sample csv's are included in repo
2. The default model is qwen2.5:0.5b (change with `--model`)
3. Each dataset will generate `report.md`, `column_profile.csv`, `analysis_summary.json`, `plots/plot_NN.png`, `llm_prompt.txt`, `llm_response.txt`, and conditionally `correlation_matrix.csv`

### Known Limitations
- Outliers using the 0.5 x IQR rule are flagged but not removed
- Does not support Excel, JSON, databases, etc.
- The number check on the model output just compares against the summary within rounding so legitimate rewording can be flagged and it cannot check if a claim is reasonable
- Flags will only be placed on simple value patterns and column names. Just because data is not flagged does not mean it is safe.
- Default LLM cannot handle large prompts so it timeouts frequently

Data Source Credits:
https://catalog.data.gov/dataset/supply-chain-greenhouse-gas-emission-factors-v1-3-by-naics-6
https://www.kaggle.com/datasets/bisheshkhanalcs26/genshin-impact?resource=download
