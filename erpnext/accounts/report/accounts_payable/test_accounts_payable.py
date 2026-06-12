import frappe
from frappe.utils import add_days, flt, today

from erpnext.accounts.doctype.purchase_invoice.test_purchase_invoice import make_purchase_invoice
from erpnext.accounts.report.accounts_payable.accounts_payable import execute
from erpnext.accounts.report.accounts_receivable.accounts_receivable import (
	make_payment_entries,
)
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.tests.utils import ERPNextTestSuite


class TestAccountsPayable(ERPNextTestSuite, AccountsTestMixin):
	def setUp(self):
		self.create_company()
		self.create_customer()
		self.create_item()
		self.create_supplier(currency="USD", supplier_name="Test Supplier2")
		self.create_usd_payable_account()

	def test_accounts_payable_for_foreign_currency_supplier(self):
		pi = self.create_purchase_invoice(do_not_submit=True)
		pi.currency = "USD"
		pi.conversion_rate = 80
		pi.credit_to = self.creditors_usd
		pi = pi.save().submit()

		filters = {
			"company": self.company,
			"party_type": "Supplier",
			"party": [self.supplier],
			"report_date": today(),
			"range": "30, 60, 90, 120",
			"in_party_currency": 1,
		}

		data = execute(filters)
		self.assertEqual(data[1][0].get("outstanding"), 300)
		self.assertEqual(data[1][0].get("currency"), "USD")

	def create_purchase_invoice(self, do_not_submit=False):
		frappe.set_user("Administrator")
		pi = make_purchase_invoice(
			item=self.item,
			company=self.company,
			supplier=self.supplier,
			is_return=False,
			update_stock=False,
			posting_date=frappe.utils.datetime.date(2021, 5, 1),
			do_not_save=1,
			rate=300,
			price_list_rate=300,
			qty=1,
		)

		pi = pi.save()
		if not do_not_submit:
			pi = pi.submit()
		return pi

	def test_payment_terms_template_filters(self):
		from erpnext.controllers.accounts_controller import get_payment_terms

		payment_term1 = frappe.get_doc(
			{"doctype": "Payment Term", "payment_term_name": "_Test 50% on 15 Days"}
		).insert()
		payment_term2 = frappe.get_doc(
			{"doctype": "Payment Term", "payment_term_name": "_Test 50% on 30 Days"}
		).insert()

		template = frappe.get_doc(
			{
				"doctype": "Payment Terms Template",
				"template_name": "_Test 50-50",
				"terms": [
					{
						"doctype": "Payment Terms Template Detail",
						"due_date_based_on": "Day(s) after invoice date",
						"payment_term": payment_term1.name,
						"description": "_Test 50-50",
						"invoice_portion": 50,
						"credit_days": 15,
					},
					{
						"doctype": "Payment Terms Template Detail",
						"due_date_based_on": "Day(s) after invoice date",
						"payment_term": payment_term2.name,
						"description": "_Test 50-50",
						"invoice_portion": 50,
						"credit_days": 30,
					},
				],
			}
		)
		template.insert()

		filters = {
			"company": self.company,
			"report_date": today(),
			"range": "30, 60, 90, 120",
			"based_on_payment_terms": 1,
			"payment_terms_template": template.name,
			"ageing_based_on": "Posting Date",
		}

		pi = self.create_purchase_invoice(do_not_submit=True)
		pi.payment_terms_template = template.name
		schedule = get_payment_terms(template.name)
		pi.set("payment_schedule", [])

		for row in schedule:
			row["due_date"] = add_days(pi.posting_date, row.get("credit_days", 0))
			pi.append("payment_schedule", row)

		pi.save()
		pi.submit()

		report = execute(filters)
		row = report[1][0]

		self.assertEqual(len(report[1]), 2)
		self.assertEqual([pi.name, payment_term1.payment_term_name], [row.voucher_no, row.payment_term])

	def test_project_filter(self):
		project = frappe.get_doc(
			{"doctype": "Project", "project_name": "_Test AP Project", "company": self.company}
		).insert()

		pi = self.create_purchase_invoice(do_not_submit=True)
		pi.project = project.name
		pi.save().submit()

		filters = {
			"company": self.company,
			"report_date": today(),
			"range": "30, 60, 90, 120",
			"project": [project.name],
		}

		report = execute(filters)[1]
		self.assertEqual(len(report), 1)
		row = report[0]
		self.assertEqual(row.project, project.name)
		self.assertEqual(row.invoiced, 300.0)

	def test_project_on_report_output(self):
		"""
		Report row must carry the invoice's project.
		"""
		filters = {
			"company": self.company,
			"report_date": today(),
			"range": "30, 60, 90, 120",
		}

		project = frappe.get_doc(
			{"doctype": "Project", "project_name": "_Test AP Project Output", "company": self.company}
		).insert()

		pi = self.create_purchase_invoice(do_not_submit=True)
		pi.project = project.name
		pi.save().submit()

		report = execute(filters)

		self.assertEqual(len(report[1]), 1)
		row = report[1][0]
		self.assertEqual([pi.name, project.name, 300], [row.voucher_no, row.project, row.outstanding])

	# --- Bulk "Create Payment Entry" from the report ----------------------------------

	def _ap_report_rows(self, party=None):
		filters = {
			"company": self.company,
			"party_type": "Supplier",
			"report_date": today(),
			"range": "30, 60, 90, 120",
		}
		if party:
			filters["party"] = [party]
		return execute(filters)[1]

	def _make_pi(self, supplier, credit_to=None, currency=None, conversion_rate=1, rate=300):
		frappe.set_user("Administrator")
		pi = make_purchase_invoice(
			item=self.item,
			company=self.company,
			supplier=supplier,
			is_return=False,
			update_stock=False,
			posting_date=frappe.utils.datetime.date(2021, 5, 1),
			do_not_save=1,
			rate=rate,
			price_list_rate=rate,
			qty=1,
		)
		if currency:
			pi.currency = currency
			pi.conversion_rate = conversion_rate
		if credit_to:
			pi.credit_to = credit_to
		return pi.save().submit()

	def test_bulk_pay_single_supplier_multiple_invoices(self):
		self.create_supplier(supplier_name="_Test AP Bulk Supplier")
		pi1 = self._make_pi(self.supplier)
		pi2 = self._make_pi(self.supplier)

		rows = [r for r in self._ap_report_rows(self.supplier) if r.get("voucher_no") in (pi1.name, pi2.name)]
		names = make_payment_entries({"company": self.company}, rows)

		self.assertEqual(len(names), 1)
		pe = frappe.get_doc("Payment Entry", names[0])
		self.assertEqual(pe.docstatus, 0)
		self.assertEqual(pe.payment_type, "Pay")
		self.assertEqual(pe.party, self.supplier)
		self.assertEqual(len(pe.references), 2)
		self.assertEqual(flt(sum(r.allocated_amount for r in pe.references)), 600)

	def test_bulk_pay_multiple_suppliers(self):
		self.create_supplier(supplier_name="_Test AP Supplier A")
		supplier_a = self.supplier
		pi_a1 = self._make_pi(supplier_a)
		pi_a2 = self._make_pi(supplier_a)

		self.create_supplier(supplier_name="_Test AP Supplier B")
		supplier_b = self.supplier
		pi_b1 = self._make_pi(supplier_b)

		wanted = {pi_a1.name, pi_a2.name, pi_b1.name}
		rows = [r for r in self._ap_report_rows() if r.get("voucher_no") in wanted]
		names = make_payment_entries({"company": self.company}, rows)

		self.assertEqual(len(names), 2)
		by_party = {frappe.db.get_value("Payment Entry", n, "party"): n for n in names}
		self.assertEqual(set(by_party), {supplier_a, supplier_b})
		self.assertEqual(len(frappe.get_doc("Payment Entry", by_party[supplier_a]).references), 2)
		self.assertEqual(len(frappe.get_doc("Payment Entry", by_party[supplier_b]).references), 1)

	def test_bulk_pay_multi_currency_splits_into_separate_entries(self):
		# self.supplier (USD) + self.creditors_usd created in setUp
		pi_usd = self._make_pi(
			self.supplier, credit_to=self.creditors_usd, currency="USD", conversion_rate=80
		)
		pi_inr = self._make_pi(self.supplier, credit_to=self.creditors, currency="INR", conversion_rate=1)

		wanted = {pi_usd.name, pi_inr.name}
		rows = [r for r in self._ap_report_rows(self.supplier) if r.get("voucher_no") in wanted]
		names = make_payment_entries({"company": self.company}, rows)

		self.assertEqual(len(names), 2)
		for n in names:
			pe = frappe.get_doc("Payment Entry", n)
			self.assertEqual(len(pe.references), 1)

	def test_bulk_pay_filters_invalid_rows(self):
		invalid_rows = [
			{"bold": 1, "party": self.supplier, "party_type": "Supplier"},
			{
				"voucher_type": "Purchase Invoice",
				"voucher_no": "PINV-INVALID",
				"party": self.supplier,
				"party_type": "Supplier",
				"outstanding": -50,
			},
			{
				"voucher_type": "Payment Entry",
				"voucher_no": "PE-INVALID",
				"party": self.supplier,
				"party_type": "Supplier",
				"outstanding": 100,
			},
		]
		self.assertRaises(
			frappe.ValidationError, make_payment_entries, {"company": self.company}, invalid_rows
		)

	def _make_term_template(self):
		# Two real Payment Terms sharing the SAME description: the report's payment_term column
		# shows identical text for both rows, so only the row's real term name can identify them.
		self.payment_term1 = frappe.get_doc(
			{"doctype": "Payment Term", "payment_term_name": "_Test Bulk 50% on 15 Days"}
		).insert()
		self.payment_term2 = frappe.get_doc(
			{"doctype": "Payment Term", "payment_term_name": "_Test Bulk 50% on 30 Days"}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Payment Terms Template",
				"template_name": "_Test Bulk 50-50",
				"allocate_payment_based_on_payment_terms": 1,
				"terms": [
					{
						"doctype": "Payment Terms Template Detail",
						"due_date_based_on": "Day(s) after invoice date",
						"payment_term": self.payment_term1.name,
						"description": "_Test Bulk 50-50",
						"invoice_portion": 50,
						"credit_days": 15,
					},
					{
						"doctype": "Payment Terms Template Detail",
						"due_date_based_on": "Day(s) after invoice date",
						"payment_term": self.payment_term2.name,
						"description": "_Test Bulk 50-50",
						"invoice_portion": 50,
						"credit_days": 30,
					},
				],
			}
		).insert()

	def _term_filters(self, based_on_payment_terms):
		filters = {
			"company": self.company,
			"party_type": "Supplier",
			"party": [self.supplier],
			"report_date": today(),
			"range": "30, 60, 90, 120",
			"ageing_based_on": "Posting Date",
		}
		if based_on_payment_terms:
			filters["based_on_payment_terms"] = 1
		return filters

	def test_bulk_pay_term_invoice_both_terms_selected(self):
		"""Filter ON: selecting both term rows -> 1 PE with 2 term references (real term names)."""
		template = self._make_term_template()
		self.create_supplier(supplier_name="_Test AP Term Supplier")
		pi = self._make_pi_with_terms(self.supplier, template.name)

		rows = [r for r in execute(self._term_filters(True))[1] if r.get("voucher_no") == pi.name]
		self.assertEqual(len(rows), 2)  # report split by payment term

		names = make_payment_entries({"company": self.company}, rows)
		self.assertEqual(len(names), 1)
		pe = frappe.get_doc("Payment Entry", names[0])
		self.assertEqual(pe.docstatus, 0)
		self.assertEqual(len(pe.references), 2)
		self.assertEqual(
			{r.payment_term for r in pe.references},
			{self.payment_term1.name, self.payment_term2.name},
		)
		self.assertEqual(flt(sum(r.allocated_amount for r in pe.references)), 300)

	def test_bulk_pay_term_invoice_single_term_selected(self):
		"""Filter ON: selecting only one term row -> 1 PE with just that term referenced."""
		template = self._make_term_template()
		self.create_supplier(supplier_name="_Test AP Term Supplier One")
		pi = self._make_pi_with_terms(self.supplier, template.name)

		rows = [r for r in execute(self._term_filters(True))[1] if r.get("voucher_no") == pi.name]
		first_term_row = sorted(rows, key=lambda r: r.get("due_date"))[0]

		names = make_payment_entries({"company": self.company}, [first_term_row])
		self.assertEqual(len(names), 1)
		pe = frappe.get_doc("Payment Entry", names[0])
		self.assertEqual(len(pe.references), 1)
		self.assertEqual(pe.references[0].payment_term, self.payment_term1.name)
		self.assertEqual(flt(pe.references[0].allocated_amount), 150)

	def test_bulk_pay_term_invoice_whole_invoice_selected(self):
		"""Filter OFF: selecting the whole invoice (no term) -> 1 PE expanded to all terms."""
		template = self._make_term_template()
		self.create_supplier(supplier_name="_Test AP Term Supplier Whole")
		pi = self._make_pi_with_terms(self.supplier, template.name)

		rows = [r for r in execute(self._term_filters(False))[1] if r.get("voucher_no") == pi.name]
		self.assertEqual(len(rows), 1)  # single invoice row, no payment_term
		self.assertFalse(rows[0].get("payment_term"))

		names = make_payment_entries({"company": self.company}, rows)
		self.assertEqual(len(names), 1)
		pe = frappe.get_doc("Payment Entry", names[0])
		self.assertEqual(len(pe.references), 2)
		self.assertTrue(all(r.payment_term for r in pe.references))
		self.assertEqual(flt(sum(r.allocated_amount for r in pe.references)), 300)

	def _make_pi_with_terms(self, supplier, template_name):
		from erpnext.controllers.accounts_controller import get_payment_terms

		frappe.set_user("Administrator")
		pi = make_purchase_invoice(
			item=self.item,
			company=self.company,
			supplier=supplier,
			is_return=False,
			update_stock=False,
			posting_date=frappe.utils.datetime.date(2021, 5, 1),
			do_not_save=1,
			rate=300,
			price_list_rate=300,
			qty=1,
		)
		pi.payment_terms_template = template_name
		pi.set("payment_schedule", [])
		for row in get_payment_terms(template_name):
			row["due_date"] = add_days(pi.posting_date, row.get("credit_days", 0))
			pi.append("payment_schedule", row)
		return pi.save().submit()

	def test_bulk_pay_honours_allocation_override(self):
		self.create_supplier(supplier_name="_Test AP Alloc Supplier")
		pi = self._make_pi(self.supplier)

		rows = [r for r in self._ap_report_rows(self.supplier) if r.get("voucher_no") == pi.name]
		rows[0]["allocated_amount"] = 100  # pay only part of the 300 outstanding

		names = make_payment_entries({"company": self.company}, rows)
		self.assertEqual(len(names), 1)
		pe = frappe.get_doc("Payment Entry", names[0])
		self.assertEqual(flt(pe.references[0].allocated_amount), 100)

	def test_bulk_pay_receivable_path(self):
		from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import execute as ar_execute

		si = create_sales_invoice(
			company=self.company,
			customer=self.customer,
			debit_to=self.debit_to,
			item=self.item,
			cost_center=self.cost_center,
			income_account=self.income_account,
			rate=100,
		)

		filters = {
			"company": self.company,
			"party_type": "Customer",
			"party": [self.customer],
			"report_date": today(),
			"range": "30, 60, 90, 120",
		}
		rows = [r for r in ar_execute(filters)[1] if r.get("voucher_no") == si.name]
		names = make_payment_entries({"company": self.company}, rows)

		self.assertEqual(len(names), 1)
		pe = frappe.get_doc("Payment Entry", names[0])
		self.assertEqual(pe.payment_type, "Receive")
		self.assertEqual(pe.party, self.customer)
