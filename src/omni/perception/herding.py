"""
Herding Behavior Analysis

Detects and analyzes herding behavior in financial markets
"""
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class HerdingAnalyzer:
    """Analyze herding behavior in market movements"""

    def __init__(self):
        """Initialize herding analyzer"""
        self.min_correlation_threshold = 0.6
        self.herding_window = 20  # Days to consider for herding

    def calculate_cssd(
        self,
        returns: pd.DataFrame,
        market_returns: pd.Series,
        extreme_threshold: float = 0.05
    ) -> pd.DataFrame:
        """
        Calculate Cross-Sectional Standard Deviation (CSSD)
        Lower values during extreme markets indicate herding

        Args:
            returns: DataFrame of asset returns
            market_returns: Series of market returns
            extreme_threshold: Percentile for extreme market definition

        Returns:
            DataFrame with CSSD values and herding indicators
        """
        # Calculate cross-sectional standard deviation
        cssd = returns.std(axis=1)

        # Identify extreme market days
        extreme_up = market_returns > market_returns.quantile(1 - extreme_threshold)
        extreme_down = market_returns < market_returns.quantile(extreme_threshold)
        extreme_days = extreme_up | extreme_down

        # Average CSSD during normal vs extreme days
        cssd_normal = cssd[~extreme_days].mean()
        cssd_extreme = cssd[extreme_days].mean()

        # Herding exists if CSSD is lower during extreme days
        herding_ratio = cssd_extreme / cssd_normal if cssd_normal > 0 else 1

        results = pd.DataFrame({
            'cssd': cssd,
            'market_return': market_returns,
            'extreme_day': extreme_days,
            'extreme_up': extreme_up,
            'extreme_down': extreme_down
        })

        results['herding_indicator'] = (cssd < cssd.rolling(60).quantile(0.25)) & extreme_days

        return results

    def calculate_csad(
        self,
        returns: pd.DataFrame,
        market_returns: pd.Series
    ) -> Dict[str, Any]:
        """
        Calculate Cross-Sectional Absolute Deviation (CSAD)
        Tests for non-linear relationship indicating herding

        Args:
            returns: DataFrame of asset returns
            market_returns: Series of market returns

        Returns:
            Dictionary with CSAD analysis results
        """
        from scipy import stats

        # Calculate CSAD
        mean_return = returns.mean(axis=1)
        csad = (returns.sub(mean_return, axis=0).abs()).mean(axis=1)

        # Prepare regression variables, aligned to the CSAD index (numpy-based
        # to stay robust against index mismatch between csad and market_returns).
        rm = np.asarray(market_returns.reindex(csad.index).values, dtype=float)
        rm_abs = np.abs(rm)
        rm_squared = rm ** 2
        csad_values = csad.values.astype(float)

        # Run regression: CSAD = α + β1|Rm| + β2Rm² + ε
        # Negative β2 indicates herding. Remove NaN rows.
        mask = ~(np.isnan(csad_values) | np.isnan(rm))
        X_features = np.column_stack([rm_abs[mask], rm_squared[mask]])
        y_clean = csad_values[mask]

        # Estimate regression
        from sklearn.linear_model import LinearRegression
        model = LinearRegression()
        model.fit(X_features, y_clean)

        beta1 = model.coef_[0]
        beta2 = model.coef_[1]
        r_squared = model.score(X_features, y_clean)

        # Test significance (simplified)
        residuals = y_clean - model.predict(X_features)
        residual_std = residuals.std()
        n = len(y_clean)

        # Standard errors (simplified)
        rm_sq_std = rm_squared[mask].std()
        se_beta2 = (
            residual_std / (np.sqrt(n) * rm_sq_std)
            if n > 0 and rm_sq_std > 0 else np.nan
        )
        t_stat = beta2 / se_beta2 if se_beta2 and not np.isnan(se_beta2) else 0.0
        p_value = 2 * (1 - stats.t.cdf(abs(t_stat), n - 3)) if n > 3 else 1.0

        return {
            'csad_current': float(csad.iloc[-1]),
            'beta1': float(beta1),
            'beta2': float(beta2),
            'beta2_tstat': float(t_stat),
            'beta2_pvalue': float(p_value),
            'r_squared': float(r_squared),
            'herding_detected': beta2 < 0 and p_value < 0.05,
            'herding_strength': abs(beta2) if beta2 < 0 else 0
        }

    def detect_correlation_clustering(
        self,
        returns: pd.DataFrame,
        window: int = 60
    ) -> Dict[str, Any]:
        """
        Detect clustering in correlation networks indicating herding

        Args:
            returns: DataFrame of asset returns
            window: Rolling window for correlation

        Returns:
            Dictionary with correlation clustering analysis
        """
        import networkx as nx
        from scipy import stats

        # Calculate rolling correlations
        current_corr = returns.iloc[-window:].corr()

        # Create correlation network
        G = nx.Graph()

        # Add edges for significant correlations
        for i in range(len(current_corr)):
            for j in range(i + 1, len(current_corr)):
                corr_value = current_corr.iloc[i, j]
                if abs(corr_value) > self.min_correlation_threshold:
                    G.add_edge(
                        current_corr.index[i],
                        current_corr.columns[j],
                        weight=abs(corr_value)
                    )

        # Calculate network metrics
        if len(G.nodes()) > 0:
            # Clustering coefficient
            clustering_coef = nx.average_clustering(G, weight='weight')

            # Find communities
            communities = list(nx.community.greedy_modularity_communities(G))

            # Largest community size
            largest_community_size = max(len(c) for c in communities) if communities else 0

            # Network density
            density = nx.density(G)

            # Average correlation
            if G.edges():
                avg_correlation = np.mean([d['weight'] for _, _, d in G.edges(data=True)])
            else:
                avg_correlation = 0
        else:
            clustering_coef = 0
            communities = []
            largest_community_size = 0
            density = 0
            avg_correlation = 0

        # Historical comparison
        historical_clustering = []
        for i in range(window, len(returns), 5):  # Sample every 5 days
            hist_corr = returns.iloc[i-window:i].corr()
            G_hist = nx.Graph()

            for i_idx in range(len(hist_corr)):
                for j_idx in range(i_idx + 1, len(hist_corr)):
                    corr_val = hist_corr.iloc[i_idx, j_idx]
                    if abs(corr_val) > self.min_correlation_threshold:
                        G_hist.add_edge(i_idx, j_idx, weight=abs(corr_val))

            if len(G_hist.nodes()) > 0:
                historical_clustering.append(nx.average_clustering(G_hist, weight='weight'))

        # Current clustering percentile
        if historical_clustering:
            clustering_percentile = stats.percentileofscore(historical_clustering, clustering_coef)
        else:
            clustering_percentile = 50

        return {
            'clustering_coefficient': float(clustering_coef),
            'clustering_percentile': float(clustering_percentile),
            'network_density': float(density),
            'average_correlation': float(avg_correlation),
            'n_communities': len(communities),
            'largest_community_size': largest_community_size,
            'total_assets': len(returns.columns),
            'connected_assets': len(G.nodes()),
            'herding_level': self._classify_herding_level(clustering_coef, clustering_percentile)
        }

    def analyze_beta_herding(
        self,
        returns: pd.DataFrame,
        market_returns: pd.Series,
        window: int = 60
    ) -> pd.DataFrame:
        """
        Analyze herding through beta convergence

        Args:
            returns: DataFrame of asset returns
            market_returns: Series of market returns
            window: Rolling window for beta calculation

        Returns:
            DataFrame with beta dispersion analysis
        """
        # Calculate rolling betas
        betas = pd.DataFrame(index=returns.index[window:])

        for col in returns.columns:
            # Rolling beta calculation
            rolling_betas = []

            for i in range(window, len(returns)):
                y = returns[col].iloc[i-window:i].values
                x = market_returns.iloc[i-window:i].values

                # Remove NaN values
                mask = ~(np.isnan(y) | np.isnan(x))
                if mask.sum() > window // 2:  # Need at least half the data
                    coef = np.cov(y[mask], x[mask])[0, 1] / np.var(x[mask])
                    rolling_betas.append(coef)
                else:
                    rolling_betas.append(np.nan)

            betas[col] = rolling_betas

        # Calculate beta dispersion
        beta_dispersion = betas.std(axis=1)
        beta_mean = betas.mean(axis=1)

        # Herding indicator: low beta dispersion
        herding_indicator = beta_dispersion < beta_dispersion.rolling(120).quantile(0.2)

        results = pd.DataFrame({
            'beta_dispersion': beta_dispersion,
            'beta_mean': beta_mean,
            'herding_indicator': herding_indicator,
            'n_assets': betas.count(axis=1)
        })

        return results

    def detect_information_cascades(
        self,
        returns: pd.DataFrame,
        volume: pd.DataFrame,
        news_counts: Optional[pd.DataFrame] = None
    ) -> List[Dict[str, Any]]:
        """
        Detect information cascades that lead to herding

        Args:
            returns: DataFrame of returns
            volume: DataFrame of volume
            news_counts: Optional news mention counts

        Returns:
            List of detected cascade events
        """
        cascades = []

        # Look for sequential adoption patterns
        for date_idx in range(20, len(returns)):
            date = returns.index[date_idx]

            # Get 20-day window
            ret_window = returns.iloc[date_idx-20:date_idx]
            vol_window = volume.iloc[date_idx-20:date_idx]

            # Identify initial movers (top performers in first 5 days)
            initial_period = ret_window.iloc[:5].sum()
            initial_movers = initial_period.nlargest(5).index

            # Check if others followed
            follower_period = ret_window.iloc[5:].sum()

            # Calculate following ratio
            initial_avg_return = initial_period[initial_movers].mean()
            other_assets = [col for col in returns.columns if col not in initial_movers]
            follower_avg_return = follower_period[other_assets].mean()

            # High follower returns indicate cascade
            if follower_avg_return > 0 and initial_avg_return > 0:
                following_ratio = follower_avg_return / initial_avg_return

                if following_ratio > 0.5:  # Followers achieved 50%+ of leaders' returns
                    # Calculate cascade strength
                    cascade_breadth = (follower_period > follower_period.quantile(0.7)).sum() / len(other_assets)

                    cascade = {
                        'date': date,
                        'initial_movers': list(initial_movers),
                        'initial_return': float(initial_avg_return),
                        'follower_return': float(follower_avg_return),
                        'following_ratio': float(following_ratio),
                        'cascade_breadth': float(cascade_breadth),
                        'affected_assets': int((follower_period > 0).sum())
                    }

                    # Add news data if available
                    if news_counts is not None:
                        news_surge = news_counts.iloc[date_idx-20:date_idx].sum().sum()
                        cascade['news_mentions'] = int(news_surge)

                    cascades.append(cascade)

        # Sort by cascade breadth
        cascades.sort(key=lambda x: x['cascade_breadth'], reverse=True)

        return cascades[:20]  # Top 20 cascades

    def calculate_herding_intensity(
        self,
        returns: pd.DataFrame,
        market_returns: pd.Series,
        method: str = 'all'
    ) -> Dict[str, Any]:
        """
        Calculate overall herding intensity using multiple methods

        Args:
            returns: DataFrame of returns
            market_returns: Market returns
            method: 'cssd', 'csad', 'correlation', 'beta', or 'all'

        Returns:
            Dictionary with herding intensity metrics
        """
        results = {}

        if method in ['cssd', 'all']:
            cssd_results = self.calculate_cssd(returns, market_returns)
            results['cssd'] = {
                'current_herding': bool(cssd_results['herding_indicator'].iloc[-1]),
                'herding_days_pct': float(cssd_results['herding_indicator'].sum() / len(cssd_results) * 100),
                'recent_herding_days': int(cssd_results['herding_indicator'].iloc[-20:].sum())
            }

        if method in ['csad', 'all']:
            csad_results = self.calculate_csad(returns, market_returns)
            results['csad'] = csad_results

        if method in ['correlation', 'all']:
            corr_results = self.detect_correlation_clustering(returns)
            results['correlation'] = corr_results

        if method in ['beta', 'all']:
            from scipy import stats

            beta_results = self.analyze_beta_herding(returns, market_returns)
            results['beta'] = {
                'current_dispersion': float(beta_results['beta_dispersion'].iloc[-1]),
                'herding_detected': bool(beta_results['herding_indicator'].iloc[-1]),
                'dispersion_percentile': float(
                    stats.percentileofscore(
                        beta_results['beta_dispersion'].dropna(),
                        beta_results['beta_dispersion'].iloc[-1]
                    )
                )
            }

        # Overall herding score (0-100)
        if method == 'all':
            scores = []

            # CSSD score
            if 'cssd' in results:
                cssd_score = results['cssd']['herding_days_pct']
                scores.append(cssd_score)

            # CSAD score
            if 'csad' in results and results['csad']['herding_detected']:
                csad_score = min(100, results['csad']['herding_strength'] * 1000)
                scores.append(csad_score)

            # Correlation score
            if 'correlation' in results:
                corr_score = results['correlation']['clustering_percentile']
                scores.append(corr_score)

            # Beta score
            if 'beta' in results:
                beta_score = 100 - results['beta']['dispersion_percentile']
                scores.append(beta_score)

            overall_score = np.mean(scores) if scores else 0

            results['overall'] = {
                'herding_score': float(overall_score),
                'herding_level': self._classify_herding_level(overall_score / 100, overall_score),
                'interpretation': self._interpret_herding_score(overall_score)
            }

        return results

    def _classify_herding_level(
        self,
        score: float,
        percentile: float
    ) -> str:
        """Classify herding level based on score and percentile"""
        if percentile >= 90 or score >= 0.8:
            return 'extreme'
        elif percentile >= 75 or score >= 0.6:
            return 'high'
        elif percentile >= 50 or score >= 0.4:
            return 'moderate'
        elif percentile >= 25 or score >= 0.2:
            return 'low'
        else:
            return 'minimal'

    def _interpret_herding_score(self, score: float) -> str:
        """Provide interpretation of herding score"""
        if score >= 80:
            return "Extreme herding detected. Market participants are moving in lockstep, indicating very low diversity of opinion."
        elif score >= 60:
            return "High herding behavior. Significant consensus in market movements with reduced independent decision-making."
        elif score >= 40:
            return "Moderate herding present. Some tendency for market participants to follow the crowd."
        elif score >= 20:
            return "Low herding behavior. Market shows reasonable diversity of opinions and strategies."
        else:
            return "Minimal herding. Market participants are acting largely independently."

    def predict_herding_persistence(
        self,
        returns: pd.DataFrame,
        current_herding_score: float,
        horizon_days: int = 20
    ) -> Dict[str, Any]:
        """
        Predict how long herding behavior will persist

        Args:
            returns: Historical returns data
            current_herding_score: Current herding intensity
            horizon_days: Prediction horizon

        Returns:
            Predictions about herding persistence
        """
        # Calculate historical herding scores
        market_returns = returns.mean(axis=1)

        # Simple herding proxy: rolling correlation average
        window = 20
        historical_herding = []

        for i in range(window, len(returns) - horizon_days):
            corr_matrix = returns.iloc[i-window:i].corr()
            avg_corr = (corr_matrix.sum().sum() - len(corr_matrix)) / (len(corr_matrix) * (len(corr_matrix) - 1))
            historical_herding.append(avg_corr)

        historical_herding = pd.Series(historical_herding)

        # Find similar historical periods
        similar_periods = np.abs(historical_herding - current_herding_score) < 0.1

        if similar_periods.sum() < 10:
            return {'error': 'Insufficient historical data'}

        # Analyze persistence after similar periods
        persistence_data = []

        for idx in np.where(similar_periods)[0]:
            # Check herding score evolution
            future_scores = []
            for j in range(1, min(horizon_days + 1, len(historical_herding) - idx)):
                future_scores.append(historical_herding.iloc[idx + j])

            if len(future_scores) >= horizon_days // 2:
                persistence_data.append({
                    'days_persisted': sum(1 for s in future_scores if s > current_herding_score * 0.8),
                    'avg_decay_rate': (future_scores[-1] - future_scores[0]) / len(future_scores) if future_scores else 0,
                    'reversal_day': next((i for i, s in enumerate(future_scores) if s < current_herding_score * 0.5), horizon_days)
                })

        if not persistence_data:
            return {'error': 'No valid historical comparisons'}

        persistence_df = pd.DataFrame(persistence_data)

        return {
            'expected_persistence_days': float(persistence_df['days_persisted'].mean()),
            'persistence_probability': {
                '5_days': float((persistence_df['days_persisted'] >= 5).mean()),
                '10_days': float((persistence_df['days_persisted'] >= 10).mean()),
                '20_days': float((persistence_df['days_persisted'] >= 20).mean())
            },
            'expected_reversal_day': float(persistence_df['reversal_day'].mean()),
            'decay_rate_per_day': float(persistence_df['avg_decay_rate'].mean()),
            'sample_size': len(persistence_data),
            'confidence': 'high' if len(persistence_data) > 50 else 'medium' if len(persistence_data) > 20 else 'low'
        }
