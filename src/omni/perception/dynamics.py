"""
Sentiment Dynamics Model

Models the evolution and impact of market sentiment on prices
"""
import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SentimentDynamicsModel:
    """Model sentiment dynamics and their market impact"""

    def __init__(self):
        """Initialize sentiment dynamics model"""
        self.sentiment_lag = 5  # Days sentiment leads/lags price

    def analyze_sentiment_price_dynamics(
        self,
        sentiment_data: pd.DataFrame,
        price_data: pd.DataFrame,
        volume_data: pd.DataFrame | None = None
    ) -> dict[str, Any]:
        """
        Analyze the dynamic relationship between sentiment and prices

        Args:
            sentiment_data: DataFrame with sentiment scores
            price_data: DataFrame with price data
            volume_data: Optional volume data

        Returns:
            Dictionary with sentiment-price dynamics analysis
        """
        results = {}

        # Ensure aligned data
        common_idx = sentiment_data.index.intersection(price_data.index)
        sentiment_aligned = sentiment_data.loc[common_idx]
        price_aligned = price_data.loc[common_idx]

        # Calculate returns
        returns = price_aligned.pct_change()

        # Analyze lead-lag relationships
        lead_lag_results = self._analyze_lead_lag(
            sentiment_aligned, returns
        )
        results['lead_lag'] = lead_lag_results

        # Sentiment momentum analysis
        sentiment_momentum = self._calculate_sentiment_momentum(
            sentiment_aligned
        )
        results['sentiment_momentum'] = sentiment_momentum

        # Sentiment divergence analysis
        divergence = self._analyze_sentiment_divergence(
            sentiment_aligned, price_aligned
        )
        results['divergence'] = divergence

        # Sentiment regime analysis
        regimes = self._identify_sentiment_regimes(
            sentiment_aligned
        )
        results['regimes'] = regimes

        # Feedback loops
        if volume_data is not None:
            volume_aligned = volume_data.loc[common_idx]
            feedback = self._analyze_feedback_loops(
                sentiment_aligned, returns, volume_aligned
            )
            results['feedback_loops'] = feedback

        # Sentiment contagion
        contagion = self._analyze_sentiment_contagion(
            sentiment_aligned
        )
        results['contagion'] = contagion

        return results

    def _analyze_lead_lag(
        self,
        sentiment: pd.DataFrame,
        returns: pd.DataFrame,
        max_lag: int = 10
    ) -> dict[str, Any]:
        """Analyze lead-lag relationships between sentiment and returns"""
        lead_lag_corr = {}

        for col in sentiment.columns:
            if col not in returns.columns:
                continue

            correlations = []

            # Test different lags
            for lag in range(-max_lag, max_lag + 1):
                if lag < 0:
                    # Sentiment leads returns
                    corr = sentiment[col].iloc[:lag].corr(
                        returns[col].iloc[-lag:]
                    )
                elif lag > 0:
                    # Returns lead sentiment
                    corr = sentiment[col].iloc[lag:].corr(
                        returns[col].iloc[:-lag]
                    )
                else:
                    # Contemporaneous
                    corr = sentiment[col].corr(returns[col])

                correlations.append({
                    'lag': lag,
                    'correlation': corr if not pd.isna(corr) else 0
                })

            # Find optimal lag
            corr_df = pd.DataFrame(correlations)
            optimal_lag = corr_df.loc[corr_df['correlation'].abs().idxmax()]

            lead_lag_corr[col] = {
                'optimal_lag': int(optimal_lag['lag']),
                'max_correlation': float(optimal_lag['correlation']),
                'sentiment_leads': optimal_lag['lag'] < 0,
                'all_correlations': correlations
            }

        # Summary statistics
        optimal_lags = [v['optimal_lag'] for v in lead_lag_corr.values()]

        return {
            'by_asset': lead_lag_corr,
            'summary': {
                'avg_optimal_lag': float(np.mean(optimal_lags)),
                'sentiment_leads_pct': float(sum(1 for l in optimal_lags if l < 0) / len(optimal_lags) * 100),
                'avg_max_correlation': float(np.mean([v['max_correlation'] for v in lead_lag_corr.values()]))
            }
        }

    def _calculate_sentiment_momentum(
        self,
        sentiment: pd.DataFrame,
        window: int = 20
    ) -> dict[str, Any]:
        """Calculate sentiment momentum indicators"""
        from scipy import stats

        momentum_data = {}

        for col in sentiment.columns:
            # Sentiment change rate
            sentiment_change = sentiment[col].diff()

            # Sentiment acceleration
            sentiment_acc = sentiment_change.diff()

            # Momentum strength (combination of level and change)
            momentum = sentiment[col] * sentiment_change

            # Persistence (how long sentiment stays in one direction)
            direction = np.sign(sentiment_change)
            persistence = (direction == direction.shift()).rolling(window).sum()

            momentum_data[col] = {
                'current_level': float(sentiment[col].iloc[-1]),
                'current_change': float(sentiment_change.iloc[-1]),
                'current_acceleration': float(sentiment_acc.iloc[-1]),
                'momentum_strength': float(momentum.iloc[-1]),
                'persistence_score': float(persistence.iloc[-1] / window),
                'trend': 'bullish' if sentiment_change.iloc[-5:].mean() > 0 else 'bearish'
            }

        # Market-wide momentum
        avg_sentiment = sentiment.mean(axis=1)
        market_momentum = avg_sentiment.diff().rolling(window).mean()

        return {
            'by_asset': momentum_data,
            'market': {
                'current_momentum': float(market_momentum.iloc[-1]),
                'momentum_percentile': float(
                    stats.percentileofscore(market_momentum.dropna(), market_momentum.iloc[-1])
                ),
                'breadth': float((sentiment.iloc[-1] > sentiment.iloc[-2]).sum() / len(sentiment.columns))
            }
        }

    def _analyze_sentiment_divergence(
        self,
        sentiment: pd.DataFrame,
        prices: pd.DataFrame,
        window: int = 20
    ) -> dict[str, Any]:
        """Identify divergences between sentiment and price"""
        divergences = []

        for col in sentiment.columns:
            if col not in prices.columns:
                continue

            # Normalize data for comparison
            sentiment_norm = (sentiment[col] - sentiment[col].rolling(window).mean()) / sentiment[col].rolling(window).std()
            price_norm = (prices[col] - prices[col].rolling(window).mean()) / prices[col].rolling(window).std()

            # Calculate divergence
            divergence = sentiment_norm - price_norm

            # Identify significant divergences
            div_threshold = divergence.rolling(window * 3).std() * 2

            # Bullish divergence: sentiment improving while price falling
            bullish_div = (sentiment_norm.diff() > 0) & (price_norm.diff() < 0) & (divergence.abs() > div_threshold)

            # Bearish divergence: sentiment worsening while price rising
            bearish_div = (sentiment_norm.diff() < 0) & (price_norm.diff() > 0) & (divergence.abs() > div_threshold)

            if bullish_div.iloc[-window:].any() or bearish_div.iloc[-window:].any():
                divergences.append({
                    'asset': col,
                    'type': 'bullish' if bullish_div.iloc[-1] else 'bearish' if bearish_div.iloc[-1] else 'none',
                    'divergence_score': float(divergence.iloc[-1]),
                    'days_persisted': int(bullish_div.iloc[-window:].sum() + bearish_div.iloc[-window:].sum()),
                    'current_sentiment': float(sentiment[col].iloc[-1]),
                    'sentiment_trend': 'up' if sentiment_norm.diff().iloc[-5:].mean() > 0 else 'down',
                    'price_trend': 'up' if price_norm.diff().iloc[-5:].mean() > 0 else 'down'
                })

        return {
            'active_divergences': divergences,
            'divergence_count': len(divergences),
            'bullish_count': sum(1 for d in divergences if d['type'] == 'bullish'),
            'bearish_count': sum(1 for d in divergences if d['type'] == 'bearish')
        }

    def _identify_sentiment_regimes(
        self,
        sentiment: pd.DataFrame,
        n_regimes: int = 3
    ) -> dict[str, Any]:
        """Identify sentiment regimes using hidden Markov model approach"""
        # Use market-wide sentiment
        market_sentiment = sentiment.mean(axis=1)

        # Simple regime identification using percentiles
        thresholds = [
            market_sentiment.quantile(0.33),
            market_sentiment.quantile(0.67)
        ]

        # Classify regimes
        regimes = pd.Series(index=market_sentiment.index, dtype=int)
        regimes[market_sentiment <= thresholds[0]] = 0  # Bearish
        regimes[(market_sentiment > thresholds[0]) & (market_sentiment <= thresholds[1])] = 1  # Neutral
        regimes[market_sentiment > thresholds[1]] = 2  # Bullish

        # Calculate regime statistics
        current_regime = int(regimes.iloc[-1])
        regime_names = ['bearish', 'neutral', 'bullish']

        # Regime persistence
        current_regime_start = None
        for i in range(len(regimes) - 1, -1, -1):
            if regimes.iloc[i] != current_regime:
                current_regime_start = i + 1
                break

        if current_regime_start is None:
            current_regime_start = 0

        days_in_regime = len(regimes) - current_regime_start

        # Transition probabilities
        transitions = {}
        for i in range(3):
            for j in range(3):
                mask = (regimes.iloc[:-1] == i) & (regimes[1:] == j)
                transitions[f'{regime_names[i]}_to_{regime_names[j]}'] = float(mask.sum() / (regimes == i).sum())

        # Regime characteristics
        regime_stats = {}
        for i, name in enumerate(regime_names):
            regime_data = sentiment[regimes == i]
            if len(regime_data) > 0:
                regime_stats[name] = {
                    'avg_sentiment': float(market_sentiment[regimes == i].mean()),
                    'volatility': float(market_sentiment[regimes == i].std()),
                    'frequency': float((regimes == i).sum() / len(regimes)),
                    'avg_duration': float(self._calculate_avg_duration(regimes == i))
                }

        return {
            'current_regime': regime_names[current_regime],
            'days_in_regime': days_in_regime,
            'regime_probabilities': {
                name: float((regimes == i).sum() / len(regimes))
                for i, name in enumerate(regime_names)
            },
            'transition_probabilities': transitions,
            'regime_characteristics': regime_stats,
            'next_regime_forecast': self._forecast_next_regime(
                transitions, regime_names[current_regime]
            )
        }

    def _calculate_avg_duration(self, regime_mask: pd.Series) -> float:
        """Calculate average duration of a regime"""
        durations = []
        in_regime = False
        duration = 0

        for val in regime_mask:
            if val and not in_regime:
                in_regime = True
                duration = 1
            elif val and in_regime:
                duration += 1
            elif not val and in_regime:
                durations.append(duration)
                in_regime = False
                duration = 0

        if in_regime:
            durations.append(duration)

        return np.mean(durations) if durations else 0

    def _forecast_next_regime(
        self,
        transitions: dict[str, float],
        current_regime: str
    ) -> dict[str, float]:
        """Forecast next regime based on transition probabilities"""
        regimes = ['bearish', 'neutral', 'bullish']
        forecast = {}

        for next_regime in regimes:
            key = f'{current_regime}_to_{next_regime}'
            forecast[next_regime] = transitions.get(key, 0)

        return forecast

    def _analyze_feedback_loops(
        self,
        sentiment: pd.DataFrame,
        returns: pd.DataFrame,
        volume: pd.DataFrame
    ) -> dict[str, Any]:
        """Analyze sentiment-price-volume feedback loops"""
        # Identify self-reinforcing patterns
        feedback_strength = {}

        for col in sentiment.columns:
            if col not in returns.columns or col not in volume.columns:
                continue

            # Positive feedback: sentiment -> returns -> volume -> sentiment
            sent_change = sentiment[col].pct_change()
            ret_next = returns[col].shift(-1)
            vol_next = volume[col].shift(-1).pct_change()
            sent_next = sentiment[col].shift(-2).pct_change()

            # Calculate feedback correlations
            sent_to_ret = sent_change.corr(ret_next)
            ret_to_vol = returns[col].corr(vol_next)
            vol_to_sent = vol_next.corr(sent_next)

            # Overall feedback strength
            feedback_score = abs(sent_to_ret * ret_to_vol * vol_to_sent)

            feedback_strength[col] = {
                'sentiment_to_returns': float(sent_to_ret) if not pd.isna(sent_to_ret) else 0,
                'returns_to_volume': float(ret_to_vol) if not pd.isna(ret_to_vol) else 0,
                'volume_to_sentiment': float(vol_to_sent) if not pd.isna(vol_to_sent) else 0,
                'feedback_strength': float(feedback_score) if not pd.isna(feedback_score) else 0,
                'feedback_type': 'positive' if feedback_score > 0 else 'negative'
            }

        # Identify assets with strong feedback loops
        strong_feedback = [
            asset for asset, data in feedback_strength.items()
            if data['feedback_strength'] > 0.1
        ]

        return {
            'by_asset': feedback_strength,
            'strong_feedback_assets': strong_feedback,
            'avg_feedback_strength': float(
                np.mean([d['feedback_strength'] for d in feedback_strength.values()])
            )
        }

    def _analyze_sentiment_contagion(
        self,
        sentiment: pd.DataFrame,
        window: int = 20
    ) -> dict[str, Any]:
        """Analyze how sentiment spreads across assets"""
        # Calculate rolling correlations
        rolling_corr = sentiment.rolling(window).corr()

        # Extract current correlations
        current_corr = sentiment.iloc[-window:].corr()

        # Identify contagion patterns
        contagion_pairs = []

        for i in range(len(current_corr)):
            for j in range(i + 1, len(current_corr)):
                asset1 = current_corr.index[i]
                asset2 = current_corr.columns[j]
                corr_value = current_corr.iloc[i, j]

                if abs(corr_value) > 0.7:  # High correlation threshold
                    # Check if correlation is increasing
                    hist_corr = []
                    for k in range(window, len(sentiment), 5):
                        period_corr = sentiment[asset1].iloc[k-window:k].corr(
                            sentiment[asset2].iloc[k-window:k]
                        )
                        hist_corr.append(period_corr)

                    if hist_corr:
                        corr_trend = np.polyfit(range(len(hist_corr)), hist_corr, 1)[0]
                    else:
                        corr_trend = 0

                    contagion_pairs.append({
                        'asset1': asset1,
                        'asset2': asset2,
                        'correlation': float(corr_value),
                        'correlation_trend': float(corr_trend),
                        'strengthening': corr_trend > 0
                    })

        # Network density (overall connectedness)
        high_corr_count = (current_corr.abs() > 0.5).sum().sum() - len(current_corr)
        possible_connections = len(current_corr) * (len(current_corr) - 1) / 2
        network_density = high_corr_count / possible_connections if possible_connections > 0 else 0

        return {
            'contagion_pairs': contagion_pairs[:10],  # Top 10 pairs
            'network_density': float(network_density),
            'avg_correlation': float(
                (current_corr.sum().sum() - len(current_corr)) / (len(current_corr) * (len(current_corr) - 1))
            ),
            'contagion_risk': 'high' if network_density > 0.6 else 'moderate' if network_density > 0.3 else 'low'
        }

    def predict_sentiment_evolution(
        self,
        sentiment_history: pd.Series,
        horizon: int = 10,
        exogenous: pd.DataFrame | None = None
    ) -> dict[str, Any]:
        """
        Predict future sentiment evolution

        Args:
            sentiment_history: Historical sentiment series
            horizon: Forecast horizon in days
            exogenous: Optional exogenous variables

        Returns:
            Sentiment predictions and confidence intervals
        """
        from scipy import stats
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        try:
            # Fit SARIMAX model
            model = SARIMAX(
                sentiment_history,
                order=(1, 0, 1),  # ARMA(1,1)
                seasonal_order=(0, 0, 0, 0),  # No seasonality
                exog=exogenous
            )

            fitted_model = model.fit(disp=False)

            # Generate forecast
            forecast = fitted_model.forecast(steps=horizon)
            forecast_ci = fitted_model.get_forecast(steps=horizon).conf_int()

            # Calculate prediction intervals
            predictions = []
            for i in range(horizon):
                predictions.append({
                    'day': i + 1,
                    'forecast': float(forecast.iloc[i]),
                    'lower_bound': float(forecast_ci.iloc[i, 0]),
                    'upper_bound': float(forecast_ci.iloc[i, 1])
                })

            # Model diagnostics
            residuals = fitted_model.resid
            ljung_box = stats.acorr_ljungbox(residuals, lags=10, return_df=True)

            return {
                'predictions': predictions,
                'model_metrics': {
                    'aic': float(fitted_model.aic),
                    'bic': float(fitted_model.bic),
                    'ljung_box_pvalue': float(ljung_box['lb_pvalue'].mean())
                },
                'trend_forecast': 'improving' if forecast.iloc[-1] > sentiment_history.iloc[-1] else 'deteriorating'
            }

        except Exception as e:
            logger.error(f"Failed to predict sentiment evolution: {e}")

            # Fallback to simple forecast
            recent_mean = sentiment_history.iloc[-10:].mean()
            recent_std = sentiment_history.iloc[-10:].std()

            predictions = []
            for i in range(horizon):
                predictions.append({
                    'day': i + 1,
                    'forecast': float(recent_mean),
                    'lower_bound': float(recent_mean - 2 * recent_std),
                    'upper_bound': float(recent_mean + 2 * recent_std)
                })

            return {
                'predictions': predictions,
                'model_metrics': {'method': 'simple_average'},
                'trend_forecast': 'stable'
            }
