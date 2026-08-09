
`pip install -r requirements.txt`


# TESTS
```
 python3 -m simulator.tests.test_option_pricer -v
 python3 -m simulator.tests.test_portfolio -v
 python3 -m simulator.tests.test_pmcc -v
 python -m simulator.tests.test_engine
 ```

 # Run PMCC

`python -m simulator.run_backtest`

`python -m simulator.run_backtest --ticker AAPL --start 2024-01-01 --end 2024-12-31`
