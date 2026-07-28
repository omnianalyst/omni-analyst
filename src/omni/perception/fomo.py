"""
FOMO (Fear of Missing Out) Detection

Identifies and quantifies FOMO behavior in market data
"""
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


class FOMODetector:
    """Detect and analyze FOMO patterns in market behavior"""

    def __init__(self):
        """Initialize FOMO detector"""
        self.scaler = StandardScaler()
        self.fomo_threshold = 0.7  # Score above this indicates FOMO

    def calculate_fomo_score(
        self,
        price_data: pd.DataFrame,
        volume_data: pd.DataFrame,
        social_sentiment: Optional[pd.DataFrame] = None,
        window: int = 20
    ) -> pd.DataFrame:
        """
        Calculate FOMO score based on multiple indicators

        Args:
            price_data: DataFrame with price data (columns: symbol prices)
            volume_data: DataFrame with volume data (matching price_data)
            social_sentiment: Optional sentiment scores
            window: Rolling window for calculations

        Returns:
            DataFrame with FOMO scores for each symbol
        """
        fomo_scores = pd.DataFrame(index=price_data.index)

        for symbol in price_data.columns:
            # Component 1: Price acceleration (rapid price increases)
            price_acceleration = self._calculate_price_acceleration(
                price_data[symbol], window
            )

            # Component 2: Volume surge
            volume_surge = self._calculate_volume_surge(
                volume_data[symbol], window
            )

            # Component 3: Momentum divergence (short-window returns outpacing long-window mean)
            momentum_divergence = self._calculate_momentum_divergence(
                price_data[symbol], window
            )

            # Component 4: Retail participation (if available)
            retail_surge = self._estimate_retail_participation(
                volume_data[symbol], price_data[symbol], window
            )

            # Component 5: Social sentiment (if available)
            if social_sentiment is not None and symbol in social_sentiment.columns:
                sentiment_score = self._calculate_sentiment_surge(
                    social_sentiment[symbol], window
                )
            else:
                sentiment_score = pd.Series(0, index=price_data.index)

            # Combine components into FOMO score
            components = pd.DataFrame({
                'price_acc': price_acceleration,
                'volume_surge': volume_surge,
                'momentum_div': momentum_divergence,
                'retail_surge': retail_surge,
                'sentiment': sentiment_score
            })

            # Normalize components
            components_normalized = pd.DataFrame(
                self.scaler.fit_transform(components.fillna(0)),
                index=components.index,
                columns=components.columns
            )

            # Weighted combination (adjust weights based on importance)
            weights = {
                'price_acc': 0.25,
                'volume_surge': 0.25,
                'momentum_div': 0.20,
                'retail_surge': 0.20,
                'sentiment': 0.10
            }

            fomo_score = sum(
                components_normalized[col] * weight
                for col, weight in weights.items()
            )

            # Normalize to 0-1 range
            fomo_scores[symbol] = self._sigmoid_normalize(fomo_score)

        return fomo_scores

    def _calculate_price_acceleration(
        self,
        prices: pd.Series,
        window: int
    ) -> pd.Series:
        """Calculate price acceleration (second derivative)"""
        returns = prices.pct_change()
        acceleration = returns.diff()

        # Rolling z-score of acceleration
        acc_mean = acceleration.rolling(window).mean()
        acc_std = acceleration.rolling(window).std()

        z_score = (acceleration - acc_mean) / (acc_std + 1e-10)

        # Only positive acceleration contributes to FOMO
        return z_score.clip(lower=0)

    def _calculate_volume_surge(
        self,
        volume: pd.Series,
        window: int
    ) -> pd.Series:
        """Calculate abnormal volume surge"""
        # Volume relative to moving average
        vol_ma = volume.rolling(window).mean()
        vol_std = volume.rolling(window).std()

        # Z-score of volume
        vol_z = (volume - vol_ma) / (vol_std + 1e-10)

        # Exponential decay for recent emphasis
        weights = np.exp(-np.arange(len(vol_z)) / window)[::-1]
        weights = weights / weights.sum()

        return vol_z.clip(lower=0)

    def _calculate_momentum_divergence(
        self,
        prices: pd.Series,
        window: int
    ) -> pd.Series:
        """Calculate divergence between price momentum and mean reversion"""
        returns = prices.pct_change()

        # Short-term momentum
        momentum_short = returns.rolling(window // 4).mean()

        # Long-term mean
        momentum_long = returns.rolling(window).mean()

        # Divergence (positive when short > long)
        divergence = momentum_short - momentum_long

        # Normalize by volatility
        vol = returns.rolling(window).std()
        normalized_div = divergence / (vol + 1e-10)

        return normalized_div.clip(lower=0)

    def _estimate_retail_participation(
        self,
        volume: pd.Series,
        prices: pd.Series,
        window: int
    ) -> pd.Series:
        """Estimate retail participation from volume patterns"""
        # Assumptions:
        # - Retail trades are smaller and more frequent
        # - Retail activity increases with price momentum

        # Price momentum
        returns = prices.pct_change()
        momentum = returns.rolling(window // 2).mean()

        # Volume volatility (retail creates more volatility)
        vol_returns = volume.pct_change()
        vol_volatility = vol_returns.rolling(window // 2).std()

        # Combine signals
        retail_signal = momentum * vol_volatility

        # Normalize
        signal_mean = retail_signal.rolling(window).mean()
        signal_std = retail_signal.rolling(window).std()

        return ((retail_signal - signal_mean) / (signal_std + 1e-10)).clip(lower=0)

    def _calculate_sentiment_surge(
        self,
        sentiment: pd.Series,
        window: int
    ) -> pd.Series:
        """Calculate surge in social sentiment"""
        # Rate of change in sentiment
        sentiment_change = sentiment.diff()

        # Acceleration of sentiment
        sentiment_acc = sentiment_change.diff()

        # Combined surge metric
        surge = sentiment_change + 0.5 * sentiment_acc

        # Normalize
        surge_mean = surge.rolling(window).mean()
        surge_std = surge.rolling(window).std()

        return ((surge - surge_mean) / (surge_std + 1e-10)).clip(lower=0)

    def _sigmoid_normalize(self, series: pd.Series) -> pd.Series:
        """Apply sigmoid normalization to map to 0-1"""
        return 1 / (1 + np.exp(-series))

    def detect_fomo_events(
        self,
        fomo_scores: pd.DataFrame,
        threshold: float = None
    ) -> List[Dict[str, Any]]:
        """
        Detect specific FOMO events from scores

        Args:
            fomo_scores: DataFrame of FOMO scores
            threshold: Score threshold for event detection

        Returns:
            List of FOMO events with details
        """
        if threshold is None:
            threshold = self.fomo_threshold

        events = []

        for symbol in fomo_scores.columns:
            scores = fomo_scores[symbol]

            # Find periods above threshold
            fomo_periods = scores > threshold

            # Group consecutive FOMO periods
            fomo_groups = (fomo_periods != fomo_periods.shift()).cumsum()

            for group_id in fomo_groups[fomo_periods].unique():
                group_mask = (fomo_groups == group_id) & fomo_periods
                group_data = scores[group_mask]

                if len(group_data) >= 3:  # Minimum 3 periods for event
                    event = {
                        'symbol': symbol,
                        'start_date': group_data.index[0],
                        'end_date': group_data.index[-1],
                        'duration_days': len(group_data),
                        'peak_score': group_data.max(),
                        'avg_score': group_data.mean(),
                        'peak_date': group_data.idxmax()
                    }
                    events.append(event)

        # Sort by peak score
        events.sort(key=lambda x: x['peak_score'], reverse=True)

        return events

    def analyze_fomo_patterns(
        self,
        price_data: pd.DataFrame,
        volume_data: pd.DataFrame,
        lookback_days: int = 90
    ) -> Dict[str, Any]:
        """
        Comprehensive FOMO pattern analysis

        Args:
            price_data: Historical price data
            volume_data: Historical volume data
            lookback_days: Days to analyze

        Returns:
            Dictionary with FOMO analysis results
        """
        from scipy import stats

        # Calculate FOMO scores
        fomo_scores = self.calculate_fomo_score(price_data, volume_data)

        # Detect events
        events = self.detect_fomo_events(fomo_scores)

        # Current FOMO levels
        current_fomo = {}
        for symbol in fomo_scores.columns:
            current_score = fomo_scores[symbol].iloc[-1]
            trend = fomo_scores[symbol].iloc[-5:].mean() - fomo_scores[symbol].iloc[-10:-5].mean()

            current_fomo[symbol] = {
                'score': float(current_score),
                'trend': 'increasing' if trend > 0 else 'decreasing',
                'percentile': float(stats.percentileofscore(fomo_scores[symbol].dropna(), current_score)),
                'status': self._get_fomo_status(current_score)
            }

        # Market-wide FOMO
        market_fomo = fomo_scores.mean(axis=1)
        current_market_fomo = market_fomo.iloc[-1]

        # Historical comparison
        historical_percentile = stats.percentileofscore(
            market_fomo.dropna(), current_market_fomo
        )

        # FOMO correlation with returns
        future_returns = price_data.pct_change(5).shift(-5)  # 5-day forward returns
        fomo_return_corr = {}

        for symbol in fomo_scores.columns:
            if symbol in future_returns.columns:
                corr = fomo_scores[symbol].corr(future_returns[symbol])
                fomo_return_corr[symbol] = float(corr) if not pd.isna(corr) else 0

        return {
            'current_levels': current_fomo,
            'recent_events': events[:10],  # Top 10 recent events
            'market_fomo': {
                'current': float(current_market_fomo),
                'percentile': float(historical_percentile),
                'trend': 'increasing' if market_fomo.iloc[-5:].mean() > market_fomo.iloc[-10:-5].mean() else 'decreasing'
            },
            'fomo_return_correlation': fomo_return_corr,
            'high_fomo_symbols': [s for s, data in current_fomo.items() if data['score'] > self.fomo_threshold]
        }

    def _get_fomo_status(self, score: float) -> str:
        """Categorize FOMO level"""
        if score >= 0.8:
            return 'extreme'
        elif score >= 0.7:
            return 'high'
        elif score >= 0.5:
            return 'moderate'
        elif score >= 0.3:
            return 'low'
        else:
            return 'minimal'

    def predict_fomo_outcomes(
        self,
        symbol: str,
        current_fomo_score: float,
        historical_data: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Predict likely outcomes based on current FOMO levels

        Args:
            symbol: Asset symbol
            current_fomo_score: Current FOMO score
            historical_data: Historical price and FOMO data

        Returns:
            Predictions and probabilities
        """
        # Find historical periods with similar FOMO scores
        fomo_scores = self.calculate_fomo_score(
            historical_data[['price']],
            historical_data[['volume']]
        )

        similar_periods = np.abs(fomo_scores[symbol] - current_fomo_score) < 0.1

        if similar_periods.sum() < 10:
            return {'error': 'Insufficient historical data'}

        # Analyze outcomes after similar FOMO levels
        outcomes = []

        for idx in np.where(similar_periods)[0]:
            if idx + 20 < len(historical_data):  # Look 20 days ahead
                start_price = historical_data['price'].iloc[idx]

                # Various outcome metrics
                returns_5d = (historical_data['price'].iloc[idx + 5] / start_price - 1) * 100
                returns_10d = (historical_data['price'].iloc[idx + 10] / start_price - 1) * 100
                returns_20d = (historical_data['price'].iloc[idx + 20] / start_price - 1) * 100

                max_drawdown = (historical_data['price'].iloc[idx:idx+20].min() / start_price - 1) * 100
                volatility = historical_data['price'].iloc[idx:idx+20].pct_change().std() * np.sqrt(252) * 100

                outcomes.append({
                    'returns_5d': returns_5d,
                    'returns_10d': returns_10d,
                    'returns_20d': returns_20d,
                    'max_drawdown': max_drawdown,
                    'volatility': volatility
                })

        if not outcomes:
            return {'error': 'No valid historical comparisons'}

        outcomes_df = pd.DataFrame(outcomes)

        # Calculate probabilities and expected outcomes
        predictions = {
            'expected_returns': {
                '5_days': float(outcomes_df['returns_5d'].mean()),
                '10_days': float(outcomes_df['returns_10d'].mean()),
                '20_days': float(outcomes_df['returns_20d'].mean())
            },
            'risk_metrics': {
                'expected_max_drawdown': float(outcomes_df['max_drawdown'].mean()),
                'expected_volatility': float(outcomes_df['volatility'].mean()),
                'worst_case_drawdown': float(outcomes_df['max_drawdown'].quantile(0.05))
            },
            'probabilities': {
                'positive_5d': float((outcomes_df['returns_5d'] > 0).mean()),
                'positive_10d': float((outcomes_df['returns_10d'] > 0).mean()),
                'positive_20d': float((outcomes_df['returns_20d'] > 0).mean()),
                'drawdown_over_10pct': float((outcomes_df['max_drawdown'] < -10).mean())
            },
            'sample_size': len(outcomes),
            'confidence': 'high' if len(outcomes) > 50 else 'medium' if len(outcomes) > 20 else 'low'
        }

        return predictions
