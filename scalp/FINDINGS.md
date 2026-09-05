# Sneaky Pivot: tested and rejected

QQQ 15-minute bars, regular trading hours, 2024-01-02 to 2026-09-03.
Alpaca offers no futures, so MNQ was substituted with QQQ shares.

## Result

```
                               CAGR     vol    maxDD   trades    win%
sneaky pivot (default params) 3.84%    8.2%    -5.9%      218     28%
QQQ buy & hold               24.46%      --       --        1      --
```

§33's full parameter sweep, 1152 combinations:

```
  916 of 1152 produced >=30 trades
  Sharpe above cash (>0):     212 of 916
  CAGR beating QQQ (24.46%):    0 of 916
```

## Out of sample: the top configurations INVERT

Fit on 2024-01-02..2025-05-05, tested on 2025-05-05..2026-09-03:

```
  #1  H1 Sharpe +0.56  ->  H2 Sharpe -0.29   H2 CAGR  2.2%
  #2  H1 Sharpe +0.53  ->  H2 Sharpe -0.45   H2 CAGR  0.8%
  #3  H1 Sharpe +0.51  ->  H2 Sharpe -0.23   H2 CAGR  2.5%
  #4  H1 Sharpe +0.51  ->  H2 Sharpe -0.27   H2 CAGR  2.5%
  #5  H1 Sharpe +0.47  ->  H2 Sharpe -0.41   H2 CAGR  1.3%

  QQQ buy & hold over the same out-of-sample period: +34.3% CAGR
```

The full-sample in/out Sharpe correlation is +0.778, which looks like
persistence. It is not. Restricted to the settings anyone would actually
choose, it inverts:

```
sample                               n     corr   mean H1   mean H2
all parameter sets                 243   +0.778     -1.26     -0.96
top 100 by in-sample               100   -0.159     -0.20     +0.27
top 50 by in-sample                 50   -0.305     +0.09     +0.19
top 20 by in-sample                 20   -0.544     +0.33     +0.10
top 10 by in-sample                 10   -0.871     +0.44     -0.02
```

The +0.778 is bad settings reliably staying bad (all-set mean Sharpe -1.26).
Among the top 10 the correlation is **-0.871**: the better a configuration
looked on history, the worse it did next. That is the signature of fitting
noise, not signal.

## Two defects in the framework as specified

- **§21's volatility filter never fires.** Identical results at
  min_range_atr 1.0 / 1.5 / 2.0 / 3.0 -- 15-minute QQQ ranges are always
  wider than 3 ATR, so the filter rejects nothing.
- **§19's trend filter hurts.** Every top-12 configuration disables it.

## Two implementation traps, both handled

- **Look-ahead.** A swing low needs the next N bars to confirm, so it is
  knowable only N bars later. Marking it at its own index lets the backtest
  buy the exact low of a move it could not have known was a low. Every swing
  carries `confirmed_at = idx + n` and no decision reads past it.
- **§23 sizes for futures margin.** `qty = risk$ / stop distance` with a $2
  stop implies 1,250 QQQ shares -- $897,012, or 3.6x the account. The first
  run refused 446 of its own 453 signals and reported on a sample of 7.
  Share count is now capped by available cash.

## Caveat

Written for MNQ: leveraged futures, 23-hour sessions, $2/point granularity.
A negative result on QQQ shares is not proof the original fails. It is the
closest test this broker allows, and it is clearly negative.
