<?xml version="1.0" encoding="UTF-8" ?>
<odoo>
    <data noupdate="1">

        <record id="bounce_reason_insufficient_funds" model="eh.cheque.bounce.reason">
            <field name="sequence">10</field>
            <field name="code">FUNDS</field>
            <field name="name">Insufficient funds</field>
            <field name="description">Drawer account did not hold sufficient balance at presentation.</field>
            <field name="is_recoverable" eval="True"/>
        </record>

        <record id="bounce_reason_signature" model="eh.cheque.bounce.reason">
            <field name="sequence">20</field>
            <field name="code">SIG</field>
            <field name="name">Signature mismatch or missing</field>
            <field name="is_recoverable" eval="True"/>
        </record>

        <record id="bounce_reason_stop_payment" model="eh.cheque.bounce.reason">
            <field name="sequence">30</field>
            <field name="code">STOP</field>
            <field name="name">Stop payment instructed</field>
            <field name="is_recoverable" eval="False"/>
        </record>

        <record id="bounce_reason_account_closed" model="eh.cheque.bounce.reason">
            <field name="sequence">40</field>
            <field name="code">CLOSED</field>
            <field name="name">Drawer account closed</field>
            <field name="is_recoverable" eval="False"/>
        </record>

        <record id="bounce_reason_post_dated" model="eh.cheque.bounce.reason">
            <field name="sequence">50</field>
            <field name="code">PDC</field>
            <field name="name">Presented before value date</field>
            <field name="is_recoverable" eval="True"/>
        </record>

        <record id="bounce_reason_amount_mismatch" model="eh.cheque.bounce.reason">
            <field name="sequence">60</field>
            <field name="code">AMT</field>
            <field name="name">Amount in words and figures differs</field>
            <field name="is_recoverable" eval="True"/>
        </record>

        <record id="bounce_reason_technical" model="eh.cheque.bounce.reason">
            <field name="sequence">70</field>
            <field name="code">TECH</field>
            <field name="name">Technical (mutilated, image quality)</field>
            <field name="is_recoverable" eval="True"/>
        </record>

        <record id="bounce_reason_other" model="eh.cheque.bounce.reason">
            <field name="sequence">99</field>
            <field name="code">OTHER</field>
            <field name="name">Other</field>
        </record>

    </data>
</odoo>
