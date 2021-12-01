import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
import pandas as pd
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import os
import argparse
import random
import copy
import time
import math
import gc

mpl.use('agg')
parser = argparse.ArgumentParser(description='PTBayeslands modelling')
parser.add_argument('-s', '--samples', help='Number of samples',
                    default=1000, dest="samples", type=int)
# parser.add_argument('-r','--replicas', help='Number of chains/replicas, best to have one per availble core/cpu', default=8,dest="num_chains",type=int)
# parser.add_argument('-t','--temperature', help='Demoninator to determine Max Temperature of chains (MT=no.chains*t) ', default=2,dest="mt_val",type=int)
# parser.add_argument('-swap','--swap', help='Swap Ratio', dest="swap_ratio",default=0.001,type=float)
parser.add_argument('-b', '--burn', help='How many samples to discard before determing posteriors',
                    dest="burn_in", default=0.5, type=float)
parser.add_argument('-lp','--l_prob',help = 'langevin_prob',dest = "l_prob", default = 0.5, type = float)
# parser.add_argument('-pt', '--ptsamples', help='Ratio of PT vs straight MCMC samples to run',
#                     dest="pt_samples", default=0.5, type=float)
parser.add_argument('-step', '--step', help='Step size for proposals (0.02, 0.05, 0.1 etc)',
                    dest="step_size", default=0.005, type=float)
parser.add_argument('-lr', '--learn', help='learn rate for langevin gradient',
                    dest="learn_rate", default=0.001, type=float)
parser.add_argument('-m', '--model', help='1 to select RNN, 2 to select LSTM',
                    dest="net", default=2, type=int)
parser.add_argument('-o', '--optim', help='1 to select SGD, 2 to select Adam',
                    dest='optimizer', default=2, type=int)
parser.add_argument('-debug', '--debug', help='debug = 0 or 1; when 1 trace plots will not be produced',
                    dest='DEBUG', default=1, type=bool)
args = parser.parse_args()

# tinker step_w to 0.05.....0.01;


class Model(nn.Module):
	# Defining input size, hidden layer size, output size and batch size respectively
	def __init__(self, topo, lrate, input_size=1, rnn_net='RNN', optimizer='SGD'):
		"""
				rnn_net = RNN/LSTM
				optimizer = Adam/ SGD
				topology = [input size, hidden size, output size]
				output size would be the same as num_classes
				num_classes =  topo[2]
				input_size = 1
				hidden_state = hidden_size = 10
		"""
		super(Model, self).__init__()
		self.hidden_dim = topo[1]  # hidden_dim
		self.input_seq_len = topo[0]
		self.num_outputs = topo[2]
		self.input_size = input_size
		self.n_layers = 1  # len(topo)-2 #n_layers
		# self.batch_size = 1
		self.lrate = lrate

		self.rnn_net = rnn_net
		if rnn_net == 'RNN':
			# self.hidden = torch.ones(self.n_layers, self.batch_size, self.hidden_dim)
			self.rnn = nn.RNN(input_size=self.input_size,
                    hidden_size=topo[1], num_layers=self.n_layers, batch_first=True)
		if rnn_net == 'LSTM':
			# self.hidden = (torch.ones((self.n_layers,self.batch_size,self.hidden_dim)), torch.ones((self.n_layers,self.batch_size,self.hidden_dim)))
			self.rnn = nn.LSTM(input_size=self.input_size,
                     hidden_size=topo[1], num_layers=self.n_layers, batch_first=True)
		# Fully connected layer
		self.fc = nn.Linear(topo[1], topo[2])
		if(optimizer == 'SGD'):
			self.optimizer = torch.optim.SGD(self.parameters(), lr = lrate)
		elif(optimizer == 'Adam'):
			self.optimizer = torch.optim.Adam(self.parameters(), lr= lrate)

		self.criterion = torch.nn.MSELoss()
		self.topo = topo
		print(rnn_net, ' is rnn net')

	def sigmoid(self, z):
		return torch.nn.Sigmoid(z)

	# vectorized version:

	def forward(self, x):
		h_0 = Variable(torch.zeros(self.n_layers, x.shape[0], self.hidden_dim))
		c_0 = Variable(torch.zeros(self.n_layers, x.shape[0], self.hidden_dim))

		batch_size = len(x)
		output_size = self.topo[2]
		# outmain = torch.zeros((batch_size, output_size, 1))

		if(self.rnn_net == 'RNN'):
			ula, h_out = self.rnn(x, h_0)
		elif(self.rnn_net == 'LSTM'):
			# ula, (h_out, _) = self.rnn(x,(h_0, c_0))
			ula, (h_out, _) = self.rnn(x)
			h_out = h_out.view(-1, self.hidden_dim)

		out = self.fc(h_out)
		out = out.view(x.shape[0], self.num_outputs, 1)
		return out  # returning the output tensor directly without detaching or deep copying

	# removing the optimzer parameter for now
	def langevin_gradient(self, x, y, w, optimizer):
		self.load_state_dict(w)
		for k in range(1):
			output = self.forward(x)
			self.optimizer.zero_grad()
			loss = self.criterion(output, y.view(output.shape))

			loss.backward()
			self.optimizer.step()

			# print(f'root of MSEloss for k = {k}: {np.sqrt(loss.detach().numpy())}')

		return copy.deepcopy(self.state_dict())

	def evaluate_proposal(self, x, w):
		backup = copy.deepcopy(self.state_dict())
		self.load_state_dict(w)
		y_pred = self.forward(x)
		self.load_state_dict(backup)
		return copy.deepcopy(y_pred.detach().numpy())

	def addnoiseandcopy(self, w, mean, std_dev):
		d = dict()
		for name in w.keys():
			d[name] = copy.deepcopy(
				w[name]) + torch.zeros(w[name].size()).normal_(mean=mean, std=std_dev)
		return d

	def getparameters(self, w=None):
		l = np.array([1, 2])
		dic = {}
		if w is None:
			dic = self.state_dict()
		else:
			dic = copy.deepcopy(w)
		for name in sorted(dic.keys()):
			l = np.concatenate(
				(l, np.array(copy.deepcopy(dic[name])).reshape(-1)), axis=None)
		l = l[2:]
		return l


class MCMC:
	def __init__(self, use_langevin_gradients, l_prob, learn_rate, samples, trainx, trainy, testx, testy, topology, path=None, rnn_net='LSTM', optimizer='Adam'):
		self.samples = samples
		self.topology = topology
		self.train_y = torch.FloatTensor(trainy)
		self.train_x = torch.FloatTensor(trainx)
		self.test_x = torch.FloatTensor(testx)
		self.test_y = torch.FloatTensor(testy)
		self.rnn_net = rnn_net
		self.path = path
		self.optimizer = optimizer
		self.use_langevin_gradients = use_langevin_gradients
		self.l_prob = l_prob
		self.learn_rate = learn_rate
		self.rnn = Model(topo=topology, lrate=learn_rate,
                   input_size=1, rnn_net=rnn_net, optimizer=optimizer)

	def rmse(self, pred, actual):
		if len(actual.shape) > 2 and actual.shape[2] == 1:
			actual = np.squeeze(actual, axis=2)
		if len(pred.shape) > 2 and pred.shape[2] == 1:
			pred = np.squeeze(pred, axis=2)
		step_wise_rmse = np.sqrt(np.mean((pred - actual)**2, axis=0))
		return np.mean(step_wise_rmse)

	def step_wise_rmse(self, pred, actual):
		if len(actual.shape) > 2 and actual.shape[2] == 1:
			actual = np.squeeze(actual, axis=2)
		if len(pred.shape) > 2 and pred.shape[2] == 1:
			pred = np.squeeze(pred, axis=2)
		step_wise_rmse = np.sqrt(np.mean((pred - actual)**2, axis=0))
		return np.sqrt(np.mean((pred - actual)**2, axis=0))

	def likelihood_func(self, rnn, x, y, w, tau_sq, temp):
		fx = rnn.evaluate_proposal(x, w)
		y = y.numpy()
		rmse = self.rmse(fx, y)
		step_wise_rmse = self.step_wise_rmse(fx, y)
		n = y.shape[0] * y.shape[1]
		p1 = -(n/2)*np.log(2*math.pi*tau_sq)
		p2 = (1/(2*tau_sq))
		# print("is this it?",(y-fx).shape)
		# print(p1.shape)
		# print(np.sum(np.square(y-fx)))
		log_lhood = p1 - (p2*np.sum(np.square(y-fx)))
		pseudo_loglhood = log_lhood/temp
		pseudo_loglhood = log_lhood
		return [pseudo_loglhood, fx, rmse, step_wise_rmse]

	def prior_likelihood(self, sigma_squared, nu_1, nu_2, w, tausq, rnn):
		h = self.topology[1]
		d = self.topology[0]
		part1 = -1 * ((len(rnn.getparameters(w))) / 2) * np.log(sigma_squared)
		part2 = 1 / (2 * sigma_squared) * \
			(sum(np.square(rnn.getparameters(w))))
		log_loss = part1 - part2 - (1 + nu_1) * np.log(tausq) - (nu_2 / tausq)
		return log_loss

	def sampler(self):
		w_size = sum(p.numel() for p in self.rnn.parameters())
		pos_w = np.ones((self.samples, w_size))
		step_wise_rmse_train = np.zeros(shape=(self.samples, 10))
		step_wise_rmse_test = np.zeros(shape=(self.samples, 10))
		rmse_train = np.zeros(self.samples)
		rmse_test = np.zeros(self.samples)
		prop_list = np.zeros((self.samples, w_size))
		likeh_list = np.zeros((self.samples, 2))
		likeh_list[0, :] = [-100, -100]
		accept_list = np.zeros(self.samples)
		num_accepted = 0
		langevin_count = 0
		pt_samples = self.samples*args.burn_in
		init_count = 0

		# new data...
		# store a list of accepted likelihoods for accepted proposals only for plotting
		likeh_list_accepted = np.zeros((self.samples, 1))
		likeh_list_accepted[0, :] = [-100]
		mh_prob_stats = np.zeros((self.samples, 4))

		if self.optimizer == 'SGD':
			optimizer = torch.optim.SGD(
				self.rnn.parameters(), lr=self.learn_rate)
		elif self.optimizer == 'Adam':
			optimizer = torch.optim.Adam(
				self.rnn.parameters(), lr=self.learn_rate)

		w = copy.deepcopy(self.rnn.state_dict())
		# w = self.rnn.state_dict()
		pred_train = self.rnn.evaluate_proposal(self.train_x, w)
		pred_test = self.rnn.evaluate_proposal(self.test_x, w)
		eta = np.log(np.var(pred_train - np.array(self.train_y)))
		tau_pro = np.exp(eta)

		eta = 0
		step_w = args.step_size
		step_eta = 0.2
		sigma_squared = 25
		nu_1 = 0
		nu_2 = 0
		print(step_w, " step_w")
		sigma_diagmat = np.zeros((w_size, w_size))
		np.fill_diagonal(sigma_diagmat, step_w**2)
		prior_current = self.prior_likelihood(
			sigma_squared, nu_1, nu_2, w, tau_pro, self.rnn)
		[likelihood, pred_train, rmsetrain, step_wise_rmsetrain] = self.likelihood_func(
			self.rnn, self.train_x, self.train_y, w, tau_pro, temp=1)
		[_, pred_test, rmsetest, step_wise_rmsetest] = self.likelihood_func(
			self.rnn, self.test_x, self.test_y, w, tau_pro, temp=1)

		scaling_factor = 1  # 0.05#1  # 0.01

		for i in range(self.samples - 1):
			timer_start = time.time()
			lx = np.random.uniform(0, 1, 1)
			old_w = copy.deepcopy(self.rnn.state_dict())
			if(self.use_langevin_gradients and lx < self.l_prob):
				w_gd = self.rnn.langevin_gradient(
					self.train_x, self.train_y, copy.deepcopy(w), optimizer=optimizer)
				w_proposal = self.rnn.addnoiseandcopy(w_gd, 0, step_w)
				w_prop_gd = self.rnn.langevin_gradient(
					self.train_x, self.train_y, copy.deepcopy(w_proposal), optimizer=optimizer)
				wc_delta = self.rnn.getparameters(
					w) - self.rnn.getparameters(w_prop_gd)
				wp_delta = self.rnn.getparameters(
					w_proposal) - self.rnn.getparameters(w_gd)
				sigma_sq = step_w*step_w
				# sigma_sq = step_w
				# this is wc_delta.T  *  wc_delta /sigma_sq
				first = -0.5 * np.sum(wc_delta * wc_delta) / sigma_sq
				second = -0.5 * np.sum(wp_delta * wp_delta) / sigma_sq
				diff_prop = (first - second)*scaling_factor
				langevin_count = langevin_count + 1
			else:
				diff_prop = 0
				w_proposal = self.rnn.addnoiseandcopy(w, 0, step_w)

			eta_pro = eta + np.random.normal(0, step_eta, 1)
			tau_pro = np.exp(eta_pro)

			[likelihood_proposal, pred_train, rmsetrain, step_wise_rmsetrain] = self.likelihood_func(
				self.rnn, self.train_x, self.train_y, copy.deepcopy(w_proposal), tau_pro, temp=1)
			[_, pred_test, rmsetest, step_wise_rmsetest] = self.likelihood_func(
				self.rnn, self.test_x, self.test_y, copy.deepcopy(w_proposal), tau_pro, temp=1)

			prior_prop = self.prior_likelihood(
				sigma_squared, nu_1, nu_2, w_proposal, tau_pro, self.rnn)
			diff_prior = prior_prop - prior_current
			diff_likelihood = likelihood_proposal - likelihood

			try:
				mh_prob = diff_likelihood + diff_prior + diff_prop
				# this is giving a slightly better accept_percentage
				mh_prob = min(1, math.exp(mh_prob))
			except OverflowError as e:
				mh_prob = 1

			accept_list[i+1] = num_accepted
			# u = np.log(random.uniform(0, 1))
			u = random.uniform(0, 1)
			# print(f'diff_likelihood: {diff_likelihood}, diff_prior: {diff_prior}, diff_prop: {diff_prop}')
			# print(mh_prob," is mh_prob and us is ",u )

			# diff_prop is value responsible for distortion, its largely negative

			mh_prob_stats[i:, ] = [diff_likelihood]
			mh_prob_stats[i:, ] = [diff_prior]
			mh_prob_stats[i:, ] = [diff_prop]
			mh_prob_stats[i:, ] = [mh_prob]
			prop_list[i+1, ] = self.rnn.getparameters(w_proposal).reshape(-1)
			likeh_list[i+1, 0] = likelihood_proposal
			if u < mh_prob:   # the correct condition is u<mh_prob
				num_accepted = num_accepted + 1
				likelihood = likelihood_proposal
				prior_current = prior_prop
				w = copy.deepcopy(w_proposal)
				eta = eta_pro
				print(i, likelihood_proposal, rmsetrain, rmsetest, ' accepted!')
				pos_w[i+1, ] = self.rnn.getparameters(w_proposal).reshape(-1)
				rmse_train[i + 1, ] = rmsetrain
				step_wise_rmse_train[i+1, ] = step_wise_rmsetrain
				rmse_test[i + 1, ] = rmsetest
				step_wise_rmse_test[i+1, ] = step_wise_rmsetest
				likeh_list_accepted[i+1, 0] = likelihood

			else:
				w = old_w
				pos_w[i+1, ] = pos_w[i, ]
				rmse_train[i + 1, ] = rmse_train[i, ]
				step_wise_rmse_train[i+1, ] = step_wise_rmse_train[i, ]
				rmse_test[i + 1, ] = rmse_test[i, ]
				step_wise_rmse_test[i+1, ] = step_wise_rmse_test[i, ]
				likeh_list_accepted[i+1, 0] = likeh_list_accepted[i, 0]

			self.rnn.load_state_dict(w)

		# pos_w is the list of all parameter_proposals- rejected as well accepted
		# rmse_train, rmse_test, step_wise_rmse_train, step_wise_rmse_test -> accepted as well rejected

		burnin = int(args.burn_in * self.samples)
		accept_percent = ((num_accepted)/self.samples)*100
		pos_w = pos_w[burnin:, :]
		rmse_train = rmse_train[burnin:]
		rmse_test = rmse_test[burnin:]
		step_wise_rmse_test = step_wise_rmse_test[burnin:, :]
		step_wise_rmse_train = step_wise_rmse_train[burnin:, :]
		likeh_list = likeh_list[burnin:, :]
		likeh_list_accepted = likeh_list_accepted[burnin:, :]
		mh_prob_stats = mh_prob_stats[burnin:, :]
		return(pos_w, rmse_train,
                    step_wise_rmse_train,
                    rmse_test, step_wise_rmse_test,
                    likeh_list, likeh_list_accepted,
                    accept_percent, mh_prob_stats,
                    langevin_count)


def main():
	n_steps_in, n_steps_out = 5, 10
	net = 'RNN' if args.net == 1 else 'LSTM'
	optimizer = 'SGD' if args.optimizer == 1 else 'Adam'
	print(f"Network is {net} and optimizer is {optimizer}")
	print("Name of folder to look for: ", os.getcwd() +
            '/Res_LG-Lprob_'+net+f'_{optimizer}_single_chain/')

	for j in [1, 2]:
		# for j in [2]:
		print(j, ' out of 15', '\n\n\n')
		#i = j//2
		problem = j
		folder = ".."
		if problem == 1:
			TrainData = pd.read_csv("./data/Lazer/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/Lazer/test1.csv", index_col=0)
			TestData = TestData.values
			name = "Lazer"
		if problem == 2:
			TrainData = pd.read_csv("./data/Sunspot/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/Sunspot/test1.csv", index_col=0)
			TestData = TestData.values
			name = "Sunspot"
		if problem == 3:
			TrainData = pd.read_csv("./data/Mackey/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/Mackey/test1.csv", index_col=0)
			TestData = TestData.values
			name = "Mackey"
		if problem == 4:
			TrainData = pd.read_csv("./data/Lorenz/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/Lorenz/test1.csv", index_col=0)
			TestData = TestData.values
			name = "Lorenz"
		if problem == 5:
			TrainData = pd.read_csv("./data/Rossler/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/Rossler/test1.csv", index_col=0)
			TestData = TestData.values
			name = "Rossler"
		if problem == 6:
			TrainData = pd.read_csv("./data/Henon/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/Henon/test1.csv", index_col=0)
			TestData = TestData.values
			name = "Henon"
		if problem == 7:
			TrainData = pd.read_csv("./data/ACFinance/train1.csv", index_col=0)
			TrainData = TrainData.values
			TestData = pd.read_csv("./data/ACFinance/test1.csv", index_col=0)
			TestData = TestData.values
			name = "ACFinance"

		print(f"Problem in evaluation: {name}")
		train_x = np.array(TrainData[:, 0:n_steps_in])
		train_y = np.array(TrainData[:, n_steps_in: n_steps_in+n_steps_out])
		test_x = np.array(TestData[:, 0:n_steps_in])
		test_y = np.array(TestData[:, n_steps_in: n_steps_in+n_steps_out])

		train_x = train_x.reshape(train_x.shape[0], train_x.shape[1], 1)
		train_y = train_y.reshape(train_y.shape[0], train_y.shape[1], 1)
		test_x = test_x.reshape(test_x.shape[0], test_x.shape[1], 1)
		test_y = test_y.reshape(test_y.shape[0], test_y.shape[1], 1)
		print("shapes of train x and y", train_x.shape, train_y.shape)

		Hidden = 10  # originally it was 5; but in the paper to compare it is 10
		topology = [n_steps_in, Hidden, n_steps_out]
		NumSample = args.samples
		netw = topology
		burn_in = args.burn_in
		learn_rate = args.learn_rate
		use_langevin_gradients = True
		print(f'Langevin is {use_langevin_gradients}')

		langevin_prob = args.l_prob # 0.9
		path = None

		# mcmc = MCMC(use_langevin_gradients, langevin_prob,
		# 			learn_rate, NumSample, train_x,
		# 			train_y, test_x, test_y,
		# 			topology,path, net, optimizer)
		if(problem == 1):
			NumSample = 10
		else:
			NumSample = args.samples
		mcmc = MCMC(use_langevin_gradients, l_prob=langevin_prob,
                    learn_rate=learn_rate, samples=NumSample, trainx=train_x,
                    trainy=train_y, testx=test_x, testy=test_y,
                    topology=topology, path=path, rnn_net=net, optimizer=optimizer)
		[pos_w, rmse_train, indiv_rmse_train, rmse_test,
                    indiv_rmse_test, likelihoods_all, accepted_likelihoods,
                    accept_percent, mh_prob_stats, langevin_count] = mcmc.sampler()
		print(pos_w.shape, " is the shape of pos_w")

		rmse_tr = np.mean(rmse_train[:])
		rmse_tr_std = np.std(rmse_train[:])
		rmse_tr_max = np.amin(rmse_train[:])
		rmse_tes = np.mean(rmse_test[:])
		rmse_tes_std = np.std(rmse_test[:])
		rmse_tes_max = np.amin(rmse_test[:])

		problemFolder = os.getcwd() + f'/SingleChain_{net}_{optimizer}/'
		folder_number = 0
		while os.path.exists(problemFolder+f'{name}_/{folder_number}'):
			folder_number += 1

		if not os.path.exists(problemFolder + f'{name}_/{folder_number}'):
			os.makedirs(problemFolder + f'{name}_/{folder_number}')
			path = (problemFolder + f'{name}_/{folder_number}')

		log_ = f"The accept percentage is: {accept_percent}\nrmse_train: {rmse_tr} rmse_train_std: {rmse_tr_std}  rmse_tr_best: {rmse_tr_max}\nrmse_test: {rmse_tes}  rmse_test_std: {rmse_tes_std}  rmse_test_best: {rmse_tes_max}\n"
		# print(f"The accept percentage is: {accept_percent}")
		# print(f'rmse_train: {rmse_tr}  rmse_train_std: {rmse_tr_std}  rmse_tr_best: {rmse_tr_max}')
		# print(f'rmse_test: {rmse_tes}  rmse_test_std: {rmse_tes_std}  rmse_test_best: {rmse_tes_max}')
		log_file = open(f"./{name}_tuning.txt", "a")
		log_file.write(f"\n\n==================={name}=================version_S\n")
		log_file.write(
			f"learning_rate = {learn_rate}; samples = {NumSample}; step_w = {args.step_size}\n")
		log_file.write(
			f"rnn_net = {net}; optimizer = {optimizer}; l_prob = {langevin_prob}")
		log_file.write(log_)
		log_file.write(f"Detailed results in folder number. {folder_number}")
		print(log_)
		log_file.close()

		directories = [
			path+'/predictions/',
			path+'/posterior',  # to store all the plots of weight distributions
			path+'/results',  # to store rmse values
			path+'/posterior/pos_w',  # to store pos_w and other plots
			path+'/posterior/pos_likelihood',  # likelihoods_all, likelihoods_accepted,
			# path+'/posterior/accept_list'
		]

		for d in directories:
			os.makedirs(d)

		"""
		what do we have right now?
			- pos_w
			- rmse_train
			- indiv_rmse_train,
			- rmse_test,
			- indiv_rmse_test, 
			- likelihoods_all, 
			- accepted_likelihoods, 
			- accept_percent,
			- mh_prob_stats
		"""

		step_wise_rmse_train_mean = np.mean(indiv_rmse_train, axis=0, keepdims=True)
		step_wise_rmse_train_std = np.std(indiv_rmse_train, axis=0, keepdims=True)
		step_wise_rmse_train_best = np.amin(indiv_rmse_train, axis=0, keepdims=True)
		step_wise_rmse_test_mean = np.mean(indiv_rmse_test, axis=0, keepdims=True)
		step_wise_rmse_test_std = np.std(indiv_rmse_test, axis=0, keepdims=True)
		step_wise_rmse_test_best = np.amin(indiv_rmse_test, axis=0, keepdims=True)

		step_wise_results = np.concatenate(
                    (
                        step_wise_rmse_train_mean,
                        step_wise_rmse_train_std,
                        step_wise_rmse_train_best,
                        step_wise_rmse_test_mean,
                        step_wise_rmse_test_std,
                        step_wise_rmse_test_best
                    ),
                    axis=0
                )

		# print(step_wise_results.shape)
		columns = [f'Step {i}' for i in range(1, 11)]
		indices = ['train_mean', 'train_std', 'train_best',
                    'test_mean', 'test_std', 'test_best']
		df = pd.DataFrame(step_wise_results, index=indices, columns=columns)
		df['overall'] = [rmse_tr, rmse_tr_std,
                   rmse_tr_max, rmse_tes, rmse_tes_std, rmse_tes_max]
		columns = ['overall'] + columns
		df = df.reindex(columns=columns)
		df = df.transpose()

		df.to_csv(path_or_buf=path + '/results/result_stepwise.csv',
		          header=f'{net} {optimizer}')

		outres_step = open(path + '/results/result_stepwise.txt', 'a+')
		resultingfile_db = open(problemFolder + '/master_result_file.txt', 'a+')
		np.savetxt(outres_step, step_wise_results, fmt='%1.4f', newline='\n')

		# allres = np.asarray([name, NumSample, langevin_prob, learn_rate,
		# 			rmse_tr, rmse_tr_std, rmse_tr_max, rmse_tes,
		# 			rmse_tes_std, rmse_tes_max, accept_percent])

		# np.savetxt(resultingfile_db, allres, newline =' ')

		print(f'{accept_percent} % samples were accepted')
		langevin_ratio = langevin_count * 100 / NumSample
		file_name = path + '/posterior/pos_w.txt'
		np.savetxt(file_name, pos_w)
		file_name = path+'/predictions/rmse_test.txt'
		np.savetxt(file_name, rmse_test, fmt='%1.8f')
		file_name = path+'/predictions/rmse_train.txt'
		np.savetxt(file_name, rmse_train, fmt='%1.8f')
		file_name = path+'/predictions/stepwise_rmse_train.txt'
		np.savetxt(file_name, indiv_rmse_train, fmt='%1.8f')
		file_name = path + '/predictions/stepwise_rmse_test.txt'
		np.savetxt(file_name, indiv_rmse_test, fmt='%1.8f')
		file_name = path+'/posterior/pos_likelihood_all.txt'
		np.savetxt(file_name, likelihoods_all, fmt='%1.4f')
		file_name = path+'/posterior/pos_likelihoods_accepted.txt'
		np.savetxt(file_name, accepted_likelihoods, fmt='%1.4f')
		# file_name = path + '/posterior/accept_list.txt'
		# np.savetxt(file_name, accept_list, fmt='%1.4f')
		# file_name = path + '/predictions/final_pred_train.txt'
		# np.savetxt(file_name, np.squeeze(pred_train), fmt='%1.4f')
		# file_name = path + '/predictions/final_pred_test.txt'
		# np.savetxt(file_name, np.squeeze(pred_test), fmt= '%1.4f')

		plt.plot(mh_prob_stats[:, 0], label='diff_likelihood')
		plt.plot(mh_prob_stats[:, 1], label='diff_prior')
		plt.plot(mh_prob_stats[:, 2], label='diff_prop')
		plt.plot(mh_prob_stats[:, 3], label='mh_prob')
		plt.legend(loc='upper right')
		plt.xlabel('Samples', fontsize=12)
		plt.ylabel('mh_prob_stats', fontsize=12)
		plt.savefig(path+'/mh_prob_stats.png')
		plt.legend(['diff_likelihood', 'diff_prior', 'diff_prop', 'mh_prob'])
		plt.clf()

		plt.plot(likelihoods_all[:, 0])
		plt.xlabel('Samples', fontsize=12)
		plt.ylabel('likelihood', fontsize=12)
		plt.savefig(path+'/likelihoods_all.png')
		plt.clf()

		plt.plot(accepted_likelihoods)
		plt.xlabel('Samples', fontsize=12)
		plt.ylabel('likelihood', fontsize=12)
		plt.savefig(path + '/likelihoods_accepted.png')
		plt.clf()

		for i in range(10):
			plt.plot(indiv_rmse_train[:, i], label=f'step {i+1}')
		plt.legend(loc='upper right')
		plt.xlabel('Samples', fontsize=12)
		plt.ylabel('Rmse', fontsize=12)
		plt.savefig(path+'/step_wise_rmse_train.png')
		plt.clf()

		for i in range(10):
			plt.plot(indiv_rmse_test[:, i], label=f'step {i+1}')
		plt.legend(loc='upper right')
		plt.xlabel('Samples', fontsize=12)
		plt.ylabel('Rmse', fontsize=12)
		plt.savefig(path+'/step_wise_rmse_test.png')
		plt.clf()

		plt.plot(rmse_train, label='train')
		plt.plot(rmse_test, label='test')
		plt.legend(loc='upper right')
		plt.xlabel('Samples', fontsize=12)
		plt.ylabel('Rmse', fontsize=12)
		plt.savefig(path + '/rmse.png')
		plt.clf()

	gc.collect()


if __name__ == "__main__":
	main()
