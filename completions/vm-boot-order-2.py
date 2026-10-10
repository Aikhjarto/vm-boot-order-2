# bash completion for vm-boot-order-2.py                     -*- shell-script -*-

_vm_boot_order_2()
{
    local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]}
    # --option=value: bash splits at the =
    if [[ $cur == = ]]; then
        cur=
    elif [[ $prev == = && $COMP_CWORD -ge 2 ]]; then
        prev=${COMP_WORDS[COMP_CWORD-2]}
    fi
    COMPREPLY=()

    case $prev in
        -c | --config)
            compopt -o filenames 2>/dev/null
            mapfile -t COMPREPLY < <(compgen -f -- "$cur")
            return
            ;;
    esac

    # the command is the first word that is neither an option nor the value
    # of -c/--config
    local i command=
    for ((i = 1; i < COMP_CWORD; i++)); do
        case ${COMP_WORDS[i]} in
            -c | --config)
                # skip the value; bash splits --config=FILE at the =
                [[ ${COMP_WORDS[i+1]} == = ]] && ((i++))
                ((i++))
                ;;
            -*)
                ;;
            *)
                command=${COMP_WORDS[i]}
                break
                ;;
        esac
    done

    local options
    case $command in
        '')
            options='-h --help -c --config -v --verbose start stop restart status'
            ;;
        start | status)
            options='-h --help'
            ;;
        stop | restart)
            options='-h --help --stop-all'
            ;;
        *)
            return
            ;;
    esac
    mapfile -t COMPREPLY < <(compgen -W "$options" -- "$cur")
}
complete -F _vm_boot_order_2 vm-boot-order-2.py
