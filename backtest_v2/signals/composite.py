from typing import Dict, Tuple, Optional
import pandas as pd
import numpy as np
from loguru import logger

from backtest_v2.signals.percentile import trailing_percentile
from backtest_v2.config import breakout_v2_config


class CompositeScorer:
    """
    Computes composite score S_raw and re-percentiles to S_tech.
    
    S_raw = 0.30*T + 0.25*M + 0.20*C + 0.25*B
    S_tech = P_252(S_raw)  <- Re-percentile the composite
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.weights = self.config.signals.weights
        self.percentile_window = self.config.signals.percentile_window
        self.s_tech_threshold = self.config.signals.s_tech_threshold
    
    def compute_S_raw(self, components: Dict[str, pd.Series]) -> pd.Series:
        """
        Compute weighted sum of percentile components.
        
        Args:
            components: Dict with keys 'T', 'M', 'C', 'B' (all in [0,1])
        
        Returns:
            S_raw series (not uniform - typically bell-shaped)
        """
        T = components.get('T', pd.Series(dtype=float))
        M = components.get('M', pd.Series(dtype=float))
        C = components.get('C', pd.Series(dtype=float))
        B = components.get('B', pd.Series(dtype=float))
        
        # Align all series to common index
        all_series = [T, M, C, B]
        common_index = T.index
        for s in all_series:
            if not s.empty:
                common_index = common_index.intersection(s.index)
        
        T = T.reindex(common_index)
        M = M.reindex(common_index)
        C = C.reindex(common_index)
        B = B.reindex(common_index)
        
        S_raw = (
            self.weights['T'] * T +
            self.weights['M'] * M +
            self.weights['C'] * C +
            self.weights['B'] * B
        )
        S_raw.name = 'S_raw'
        return S_raw
    
    def compute_S_tech(self, S_raw: pd.Series) -> pd.Series:
        """
        Re-percentile the composite score.
        
        This is critical: S_raw is a weighted sum of ~uniform variables,
        which produces a bell-shaped distribution. Re-percentiling makes
        S_tech truly uniform so threshold 0.75 = top quartile.
        """
        S_tech = trailing_percentile(S_raw, self.percentile_window)
        S_tech.name = 'S_tech'
        return S_tech
    
    def compute_all(self, components: Dict[str, pd.Series]) -> Tuple[pd.Series, pd.Series]:
        """Compute both S_raw and S_tech."""
        S_raw = self.compute_S_raw(components)
        S_tech = self.compute_S_tech(S_raw)
        return S_raw, S_tech
    
    def diagnose_S_raw_distribution(self, S_raw: pd.Series) -> Dict:
        """
        Diagnostic: prove S_raw is bell-shaped, not uniform.
        
        Returns dict with:
        - mean, std, skew, kurtosis
        - P(S_raw > 0.75) - should be ~4-5% not 25%
        - histogram bins
        """
        valid = S_raw.dropna()
        if len(valid) == 0:
            return {"error": "No valid data"}
        
        p_above_75 = (valid > 0.75).mean()
        p_above_90 = (valid > 0.90).mean()
        
        return {
            "count": int(len(valid)),
            "mean": float(valid.mean()),
            "std": float(valid.std()),
            "skew": float(valid.skew()),
            "kurtosis": float(valid.kurtosis()),
            "min": float(valid.min()),
            "max": float(valid.max()),
            "p_above_075": float(p_above_75),
            "p_above_090": float(p_above_90),
            "theoretical_p_above_075": 0.045,  # ~4.5% for weighted sum of uniforms
            "median": float(valid.median()),
        }
    
    def diagnose_S_tech_distribution(self, S_tech: pd.Series) -> Dict:
        """
        Diagnostic: verify S_tech is approximately uniform.
        """
        valid = S_tech.dropna()
        if len(valid) == 0:
            return {"error": "No valid data"}
        
        # Uniform should have mean~0.5, std~0.289, skew~0
        return {
            "count": int(len(valid)),
            "mean": float(valid.mean()),
            "std": float(valid.std()),
            "skew": float(valid.skew()),
            "p_above_075": float((valid > 0.75).mean()),
            "p_above_090": float((valid > 0.90).mean()),
            "expected_mean": 0.5,
            "expected_std": 0.289,
        }
    
    def filter_signals(self, S_tech: pd.Series) -> pd.Series:
        """Return boolean mask for S_tech > threshold."""
        return S_tech > self.s_tech_threshold


def plot_distributions(S_raw: pd.Series, S_tech: pd.Series, save_path: str = None):
    """Plot S_raw vs S_tech distributions for visual verification."""
    import matplotlib.pyplot as plt
    
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    
    # S_raw histogram
    valid_raw = S_raw.dropna()
    axes[0, 0].hist(valid_raw, bins=50, density=True, alpha=0.7, edgecolor='black')
    axes[0, 0].axvline(0.75, color='red', linestyle='--', label='Threshold 0.75')
    axes[0, 0].set_title(f'S_raw Distribution (n={len(valid_raw)})')
    axes[0, 0].set_xlabel('S_raw')
    axes[0, 0].set_ylabel('Density')
    axes[0, 0].legend()
    
    # S_raw Q-Q plot vs normal
    from scipy import stats
    stats.probplot(valid_raw, dist="norm", plot=axes[0, 1])
    axes[0, 1].set_title('S_raw Q-Q Plot vs Normal')
    
    # S_tech histogram
    valid_tech = S_tech.dropna()
    axes[1, 0].hist(valid_tech, bins=50, density=True, alpha=0.7, edgecolor='black')
    axes[1, 0].axvline(0.75, color='red', linestyle='--', label='Threshold 0.75')
    axes[1, 0].axhline(1.0, color='green', linestyle='--', label='Uniform density')
    axes[1, 0].set_title(f'S_tech Distribution (n={len(valid_tech)})')
    axes[1, 0].set_xlabel('S_tech')
    axes[1, 0].set_ylabel('Density')
    axes[1, 0].legend()
    
    # S_tech Q-Q plot vs uniform
    stats.probplot(valid_tech, dist="uniform", plot=axes[1, 1])
    axes[1, 1].set_title('S_tech Q-Q Plot vs Uniform')
    
    plt.tight_layout()
    
    if save_path:
        plt.savefig(save_path, dpi=150)
        logger.info(f"Saved distribution plots to {save_path}")
    
    return fig


if __name__ == "__main__":
    # Quick test with synthetic data
    np.random.seed(42)
    n = 1000
    
    # Simulate ~uniform components
    T = pd.Series(np.random.uniform(0, 1, n))
    M = pd.Series(np.random.uniform(0, 1, n))
    C = pd.Series(np.random.uniform(0, 1, n))
    B = pd.Series(np.random.uniform(0, 1, n))
    
    scorer = CompositeScorer()
    S_raw, S_tech = scorer.compute_all({'T': T, 'M': M, 'C': C, 'B': B})
    
    print("S_raw diagnostics:")
    print(scorer.diagnose_S_raw_distribution(S_raw))
    print("\nS_tech diagnostics:")
    print(scorer.diagnose_S_tech_distribution(S_tech))