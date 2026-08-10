
# Build
`pip install -r requirements.txt`

# TESTS
```
 python3 -m simulator.tests.test_option_pricer -v
 python3 -m simulator.tests.test_portfolio -v
 python3 -m simulator.tests.test_pmcc -v
 python -m simulator.tests.test_engine
 ```

 # Run PMCC

Running a fully automated backtest
```
python -m simulator.run_backtest

python -m simulator.run_backtest --ticker AAPL --start 2024-01-01 --end 2024-12-31
```

Running an interactive session : Note you need to adapt this script to add your own entry / exit / adjust logic , here is the template:

```
python3 -m simulator.try_session
```
