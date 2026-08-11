import sys
from loguru import logger

# Import the FinBERT loader from our news tools
try:
    from agents.tools.news_tools import get_finbert
except ImportError as e:
    logger.error(f"Failed to import from agents.tools.news_tools: {e}")
    sys.exit(1)

def test_finbert():
    logger.info("Initializing FinBERT test...")
    try:
        # Load the model (this downloads the 400MB model if not cached)
        finbert = get_finbert()
        logger.info("FinBERT model loaded successfully!")
    except Exception as e:
        logger.error(f"Failed to load FinBERT model: {e}")
        return
    
    # Test headlines ranging from positive to extremely negative (governance issues)
    headlines = [
        "Company XYZ reports record high quarterly profits, exceeding analyst expectations.", # Expected Positive
        "CEO of ABC Corp resigns abruptly amidst allegations of financial fraud and SEBI probe.", # Expected Negative (Red Flag)
        "The company announced its new product lineup for the upcoming quarter.", # Expected Neutral
        "Markets rally as inflation cools down faster than expected.", # Expected Positive
        "Massive factory fire destroys 30% of inventory for leading auto manufacturer.", # Expected Negative
        "Auditor resigns citing inability to obtain sufficient appropriate audit evidence.", # Expected Negative (Governance Red Flag)
        "Promoter pledges additional 15% stake to secure short-term bridge financing.", # Expected Negative
    ]
    
    logger.info("\n--- Running FinBERT Inference ---")
    for headline in headlines:
        try:
            # inference returns a list of dictionaries like: [{'label': 'positive', 'score': 0.95}]
            result = finbert(headline)[0]
            label = result['label']
            score = result['score']
            
            # Our sentiment agent red flag threshold is negative with > 0.85 confidence
            is_red_flag = (label == "negative" and score > 0.85)
            flag_str = "[RED FLAG]" if is_red_flag else "          "
            
            print(f"\n{flag_str} Headline: '{headline}'")
            print(f"           Verdict : {label.upper()} (Confidence: {score:.4f})")
            
        except Exception as e:
            logger.error(f"Inference failed for headline '{headline}': {e}")

if __name__ == "__main__":
    test_finbert()
