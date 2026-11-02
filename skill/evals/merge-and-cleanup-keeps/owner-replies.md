# Owner replies

`_runner/run_case.sh` sends these in order, one after each turn whose final
reply contains a question mark, by resuming the run's session. It stops when
they run out or a turn ends with no question. The text before the first colon
on each line says which question the reply is for and is not sent.

1. To a question after the merge: Leave it as it is.
